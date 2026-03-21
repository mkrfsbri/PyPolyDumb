"""
Polymarket orderbook depth feed.

Aggregates real-time orderbook data from the WebSocket manager
and provides computed metrics for strategy use:
  - mid price
  - spread
  - orderflow imbalance (buy pressure vs sell pressure)
  - flash crash detection
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from core.websocket_manager import PolymarketWebSocket, RealtimeBook

log = logging.getLogger(__name__)


@dataclass
class OrderbookMetrics:
    token_id: str
    mid_price: float = 0.50
    best_bid: float = 0.0
    best_ask: float = 1.0
    spread: float = 1.0
    imbalance: float = 0.0     # +1 = all buys, -1 = all sells
    timestamp: float = field(default_factory=time.time)

    def implied_probability(self) -> float:
        return self.mid_price


class OrderbookFeed:
    """
    Wraps PolymarketWebSocket and computes richer metrics.
    Detects flash crashes by tracking mid-price history.
    """

    def __init__(self, ws_manager: PolymarketWebSocket, crash_window_secs: float = 10.0):
        self._ws = ws_manager
        self._crash_window = crash_window_secs
        self._metrics: dict[str, OrderbookMetrics] = {}
        # Price history per token: deque of (timestamp, mid_price)
        self._price_history: dict[str, deque] = {}
        self._crash_callbacks: list = []

        # Register our update handler
        self._ws.on_update(self._on_book_update)

    def on_flash_crash(self, callback):
        """
        Register callback: def callback(token_id: str, crash_pct: float, direction: str)
        direction: 'UP' or 'DOWN' (which token crashed)
        """
        self._crash_callbacks.append(callback)

    def get_metrics(self, token_id: str) -> Optional[OrderbookMetrics]:
        return self._metrics.get(token_id)

    def implied_prob(self, token_id: str) -> float:
        m = self._metrics.get(token_id)
        return m.mid_price if m else 0.50

    def spread(self, token_id: str) -> float:
        m = self._metrics.get(token_id)
        return m.spread if m else 1.0

    def orderflow_imbalance(self, token_id: str) -> float:
        """Returns -1 to +1 imbalance score."""
        m = self._metrics.get(token_id)
        return m.imbalance if m else 0.0

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_book_update(self, token_id: str, book: RealtimeBook):
        bid = book.best_bid() or 0.0
        ask = book.best_ask() or 1.0
        mid = book.mid_price() or 0.50
        spread = ask - bid if ask > bid else 1.0

        # Compute imbalance from top-of-book depth
        bid_depth = sum(s for p, s in book.bids.items() if p >= bid - 0.02)
        ask_depth = sum(s for p, s in book.asks.items() if p <= ask + 0.02)
        total_depth = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0

        metrics = OrderbookMetrics(
            token_id=token_id,
            mid_price=mid,
            best_bid=bid,
            best_ask=ask,
            spread=spread,
            imbalance=imbalance,
        )
        self._metrics[token_id] = metrics

        # Track price history for crash detection
        if token_id not in self._price_history:
            self._price_history[token_id] = deque(maxlen=200)
        self._price_history[token_id].append((time.time(), mid))

        # Check for flash crash
        self._check_flash_crash(token_id, mid)

    def _check_flash_crash(self, token_id: str, current_mid: float):
        history = self._price_history.get(token_id)
        if not history or len(history) < 2:
            return

        now = time.time()
        cutoff = now - self._crash_window

        # Find the highest price in the crash window
        window_prices = [p for ts, p in history if ts >= cutoff]
        if not window_prices:
            return

        peak = max(window_prices)
        if peak <= 0:
            return

        drop_pct = (peak - current_mid) / peak

        if drop_pct >= 0.25:  # 25% drop threshold
            log.warning("FLASH CRASH detected: %s dropped %.1f%% in %.0fs",
                        token_id[:8], drop_pct * 100, self._crash_window)
            for cb in self._crash_callbacks:
                try:
                    cb(token_id, drop_pct, "detected")
                except Exception as e:
                    log.warning("Flash crash callback error: %s", e)
