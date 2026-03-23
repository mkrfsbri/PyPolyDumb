"""
Polymarket CLOB WebSocket manager.

Subscribes to the 'market' channel for real-time orderbook updates
on the current BTC up/down token pair.

Message types handled:
  - book      : full orderbook snapshot
  - price_change : incremental price update
  - last_trade_price : last trade
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import websockets

import config

log = logging.getLogger(__name__)


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class RealtimeBook:
    token_id: str
    bids: dict = field(default_factory=dict)   # price -> size
    asks: dict = field(default_factory=dict)
    last_update: float = field(default_factory=time.time)

    def best_bid(self) -> Optional[float]:
        if not self.bids:
            return None
        return max(self.bids.keys())

    def best_ask(self) -> Optional[float]:
        if not self.asks:
            return None
        return min(self.asks.keys())

    def mid_price(self) -> Optional[float]:
        b = self.best_bid()
        a = self.best_ask()
        if b is not None and a is not None:
            return (b + a) / 2.0
        return None

    def apply_snapshot(self, bids_raw: list, asks_raw: list):
        self.bids = {float(b["price"]): float(b["size"]) for b in bids_raw}
        self.asks = {float(a["price"]): float(a["size"]) for a in asks_raw}
        self.last_update = time.time()

    def apply_price_change(self, side: str, price: float, size: float):
        book = self.bids if side.upper() == "BUY" else self.asks
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.last_update = time.time()


class PolymarketWebSocket:
    """
    Manages a single WebSocket connection to Polymarket's market feed.
    Maintains RealtimeBook for each subscribed token.
    """

    def __init__(self):
        self._books: dict[str, RealtimeBook] = {}
        self._subscribed_tokens: set[str] = set()
        self._callbacks: list[Callable] = []
        self._ws = None
        self._running = False
        self._connected = False

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, *token_ids: str):
        """Add token IDs to subscribe on next connect / send subscribe msg."""
        for tid in token_ids:
            self._subscribed_tokens.add(tid)
            if tid not in self._books:
                self._books[tid] = RealtimeBook(token_id=tid)

    def unsubscribe(self, *token_ids: str):
        for tid in token_ids:
            self._subscribed_tokens.discard(tid)

    def get_book(self, token_id: str) -> Optional[RealtimeBook]:
        return self._books.get(token_id)

    def on_update(self, callback: Callable):
        """Register callback: def callback(token_id: str, book: RealtimeBook)"""
        self._callbacks.append(callback)

    def _clear_books(self):
        """Clear all orderbook state on disconnect so stale prices aren't used."""
        for book in self._books.values():
            book.bids.clear()
            book.asks.clear()
        log.debug("Orderbooks cleared after WS disconnect")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        """Connect and maintain WebSocket with exponential backoff reconnect."""
        self._running = True
        backoff = 2

        while self._running:
            try:
                async with websockets.connect(config.POLY_WS) as ws:
                    self._ws = ws
                    self._connected = True
                    backoff = 2  # reset on success
                    log.info("Polymarket WS connected")

                    await self._send_subscribe(ws)
                    await self._receive_loop(ws)

            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                self._connected = False
                self._clear_books()
                log.warning("Polymarket WS disconnected: %s — reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                self._connected = False
                self._clear_books()
                log.error("Polymarket WS error: %s — reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _send_subscribe(self, ws):
        """Send subscription message for all current tokens."""
        if not self._subscribed_tokens:
            return
        assets_ids = list(self._subscribed_tokens)
        msg = {
            "type": "subscribe",
            "channel": "market",
            "auth": {},
            "assets_ids": assets_ids,
        }
        await ws.send(json.dumps(msg))
        log.debug("Sent WS subscribe for %d tokens", len(assets_ids))

    async def _receive_loop(self, ws):
        """Process incoming messages."""
        async for raw in ws:
            if not self._running:
                break
            try:
                messages = json.loads(raw)
                if isinstance(messages, dict):
                    messages = [messages]
                for msg in messages:
                    self._handle_message(msg)
            except json.JSONDecodeError:
                pass
            except Exception as e:
                log.warning("WS message error: %s", e)

    def _handle_message(self, msg: dict):
        event_type = msg.get("event_type") or msg.get("type") or ""
        token_id = msg.get("asset_id") or msg.get("token_id") or ""

        if not token_id or token_id not in self._books:
            return

        book = self._books[token_id]

        if event_type == "book":
            bids = msg.get("bids", [])
            asks = msg.get("asks", [])
            book.apply_snapshot(bids, asks)
            self._fire_callbacks(token_id, book)

        elif event_type == "price_change":
            changes = msg.get("changes", [])
            for change in changes:
                side = change.get("side", "")
                price = float(change.get("price", 0))
                size = float(change.get("size", 0))
                book.apply_price_change(side, price, size)
            self._fire_callbacks(token_id, book)

    def _fire_callbacks(self, token_id: str, book: RealtimeBook):
        for cb in self._callbacks:
            try:
                cb(token_id, book)
            except Exception as e:
                log.warning("WS callback error: %s", e)

    def stop(self):
        self._running = False
