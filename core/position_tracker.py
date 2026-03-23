"""
Position tracker — tracks open positions per window and realized P&L.

Detects when a market resolves (via Gamma API or time-based heuristic)
and settles positions to realized P&L.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

import config
from core.market_discovery import MarketInfo

log = logging.getLogger(__name__)


@dataclass
class Position:
    window_slug: str
    token_id: str
    direction: str      # UP | DOWN
    strategy: str
    shares: float
    entry_price: float
    size_usdc: float
    window_close_ts: float = 0.0   # unix ts when the window ends
    opened_at: float = field(default_factory=time.time)
    closed: bool = False
    outcome: str = "PENDING"   # WIN | LOSS | PUSH | PENDING
    exit_price: float = 0.0
    realized_pnl: float = 0.0

    def unrealized_pnl(self, current_price: float) -> float:
        if self.closed:
            return 0.0
        return self.shares * (current_price - self.entry_price)

    def win_pnl(self) -> float:
        return self.shares * (1.0 - self.entry_price)

    def loss_pnl(self) -> float:
        return -self.size_usdc


@dataclass
class WindowResult:
    slug: str
    direction: str      # UP | DOWN  (winning side)
    resolved_at: float


class PositionTracker:
    """
    Maintains all open and closed positions.
    Listens for market resolutions and settles positions.
    """

    def __init__(self):
        self._positions: list[Position] = []
        self._resolved_windows: dict[str, WindowResult] = {}
        self._pnl_callbacks: list = []
        self._running = False

        self.total_realized_pnl: float = 0.0
        self.daily_pnl: float = 0.0
        self._day_start: float = self._start_of_day()

    def on_pnl_update(self, callback):
        """async def callback(position: Position, pnl: float)"""
        self._pnl_callbacks.append(callback)

    def add_position(self, position: Position):
        self._positions.append(position)
        # Check if this window already resolved
        if position.window_slug in self._resolved_windows:
            result = self._resolved_windows[position.window_slug]
            self._settle(position, result)

    def open_positions(self, window_slug: Optional[str] = None) -> list[Position]:
        return [
            p for p in self._positions
            if not p.closed
            and (window_slug is None or p.window_slug == window_slug)
        ]

    def closed_positions(self) -> list[Position]:
        return [p for p in self._positions if p.closed]

    def summary(self) -> dict:
        wins = sum(1 for p in self._positions if p.outcome == "WIN")
        losses = sum(1 for p in self._positions if p.outcome == "LOSS")
        total = wins + losses
        return {
            "total_realized_pnl": self.total_realized_pnl,
            "daily_pnl": self._current_daily_pnl(),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total if total else 0.0,
            "open_positions": len(self.open_positions()),
        }

    # ── Resolution & settlement ───────────────────────────────────────────────

    async def run(self):
        """Background loop — polls for market resolutions."""
        self._running = True
        log.info("PositionTracker started")

        while self._running:
            try:
                await self._check_resolutions()
                self._reset_daily_pnl_if_needed()
            except Exception as e:
                log.error("PositionTracker error: %s", e)
            await asyncio.sleep(10)

    # Seconds after window_close_ts before we try price-based resolution.
    _PRICE_RESOLVE_GRACE = 30
    # Seconds after window_close_ts before we force-settle as PUSH.
    # 300s = 5 min: gives Gamma API / CLOB time to propagate settlement prices.
    _PUSH_EXPIRE_GRACE = 300

    async def _check_resolutions(self):
        """Check Gamma API for resolved markets with open positions."""
        pending_slugs = {p.window_slug for p in self._positions
                         if not p.closed and p.window_slug not in self._resolved_windows}
        if not pending_slugs:
            return

        # Build window_close_ts per slug so _fetch_resolution can do time-gated
        # price-based resolution without needing the active/closed API flag.
        slug_close_ts: dict[str, float] = {}
        for p in self._positions:
            if p.window_slug in pending_slugs and p.window_close_ts > 0:
                slug_close_ts[p.window_slug] = p.window_close_ts

        # Collect token IDs per slug so CLOB fallback can query prices directly.
        slug_tokens: dict[str, list[str]] = {}
        for p in self._positions:
            if p.window_slug in pending_slugs:
                slug_tokens.setdefault(p.window_slug, [])
                if p.token_id and p.token_id not in slug_tokens[p.window_slug]:
                    slug_tokens[p.window_slug].append(p.token_id)

        async with aiohttp.ClientSession() as session:
            for slug in pending_slugs:
                try:
                    close_ts = slug_close_ts.get(slug, 0.0)
                    token_ids = slug_tokens.get(slug, [])
                    result = await self._fetch_resolution(slug, session, close_ts, token_ids)
                    if result:
                        self._resolved_windows[slug] = result
                        self._settle_window(slug, result)
                except Exception as e:
                    log.warning("Resolution check failed for %s: %s", slug, e)

        # Fallback: expire positions that are past their window close time.
        self._expire_stale_positions()

    def _expire_stale_positions(self):
        """Settle positions for windows that closed long ago but never resolved."""
        now = time.time()
        for p in self._positions:
            if p.closed or p.window_close_ts == 0:
                continue
            if p.window_slug in self._resolved_windows:
                continue
            age_past_close = now - p.window_close_ts
            if age_past_close < self._PUSH_EXPIRE_GRACE:
                continue
            log.warning(
                "Force-expiring position %s/%s — window closed %.0fs ago, API never resolved",
                p.strategy, p.window_slug, age_past_close,
            )
            result = WindowResult(
                slug=p.window_slug, direction="PUSH", resolved_at=now
            )
            self._resolved_windows[p.window_slug] = result
            self._settle_window(p.window_slug, result)

    async def _fetch_resolution(
        self, slug: str, session: aiohttp.ClientSession,
        window_close_ts: float = 0.0,
        token_ids: list = None,
    ) -> Optional[WindowResult]:
        """Try three resolution paths in order:
        1. Gamma API — explicit resolved flag (price >= 0.95 on winning token)
        2. Gamma API — price-based after window expiry (same threshold)
        3. CLOB REST — direct token price query (most up-to-date)
        """
        now = time.time()
        window_expired = (
            (window_close_ts > 0 and now >= window_close_ts + self._PRICE_RESOLVE_GRACE)
        )

        # ── Gamma API ─────────────────────────────────────────────────────────
        url = f"{config.GAMMA_API}/markets"
        try:
            async with session.get(url, params={"slug": slug},
                                   timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        market = data[0] if isinstance(data, list) else data
                        result = self._parse_gamma_resolution(slug, market, window_expired)
                        if result:
                            return result
                        # Update window_expired using API active/closed flags too
                        if not market.get("active", True) or market.get("closed", False):
                            window_expired = True
        except Exception as e:
            log.debug("Gamma resolution fetch error for %s: %s", slug, e)

        # ── CLOB REST fallback ────────────────────────────────────────────────
        # Only after window has closed — query each token's mid price directly.
        if window_expired and token_ids:
            result = await self._clob_resolution(slug, session, token_ids)
            if result:
                return result

        if window_expired:
            log.debug("Resolution: %s window expired but no settled price yet", slug)
        return None

    def _parse_gamma_resolution(
        self, slug: str, market: dict, window_expired: bool,
    ) -> Optional[WindowResult]:
        """Extract resolution from a Gamma API market dict.

        Handles:
          Shape A — tokens[]: [{tokenId, outcome, price}, ...]
          Shape B — clobTokenIds[] paired with outcomes[] / outcomePrices[]
        Threshold 0.95 (not 0.99) to handle Gamma price-feed lag.
        """
        # ── Collect all tokens (same logic as market_discovery) ───────────────
        all_tokens = []
        tokens = market.get("tokens") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = []
        if isinstance(tokens, list) and tokens:
            all_tokens = list(tokens)

        if not all_tokens:
            # Shape B: clobTokenIds + outcomes + outcomePrices
            clob_ids = market.get("clobTokenIds") or []
            outcomes = market.get("outcomes") or []
            prices = market.get("outcomePrices") or []
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    clob_ids = []
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = []
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    prices = []
            for i, tid in enumerate(clob_ids):
                all_tokens.append({
                    "tokenId": tid,
                    "outcome": outcomes[i] if i < len(outcomes) else "",
                    "price": prices[i] if i < len(prices) else "0",
                })

        # ── Check if market is considered resolved ─────────────────────────────
        resolved_flag = market.get("resolved") or market.get("is_resolved") or False
        not_active = not market.get("active", True) or market.get("closed", False)
        should_check = resolved_flag or not_active or window_expired

        if not should_check:
            return None

        # ── Find winner by price ≥ 0.95 ────────────────────────────────────────
        for token in all_tokens:
            try:
                price = float(token.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if price >= 0.95:
                outcome = (token.get("outcome") or "").strip().upper()
                direction = "UP" if ("UP" in outcome or outcome in ("YES", "HIGHER", "ABOVE")) else "DOWN"
                path = "resolved flag" if resolved_flag else ("closed flag" if not_active else "price-based")
                log.info("Resolution (%s): %s → %s (price=%.3f)", path, slug, direction, price)
                return WindowResult(slug=slug, direction=direction, resolved_at=time.time())

        return None

    async def _clob_resolution(
        self, slug: str, session: aiohttp.ClientSession, token_ids: list,
    ) -> Optional[WindowResult]:
        """Query CLOB REST /book endpoint for each token to determine winner.

        After resolution, the winning token's best_bid jumps to ≥ 0.90.
        The losing token's best_bid falls to ≤ 0.10.
        """
        url = f"{config.CLOB_HOST}/book"
        for token_id in token_ids:
            try:
                async with session.get(url, params={"token_id": token_id},
                                       timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        continue
                    book = await resp.json()
                    # CLOB book: {"bids": [{"price": "0.95", "size": "100"}, ...], "asks": [...]}
                    bids = book.get("bids") or []
                    asks = book.get("asks") or []
                    best_bid = max((float(b.get("price") or 0) for b in bids), default=0.0)
                    best_ask = min((float(a.get("price") or 1) for a in asks), default=1.0)
                    mid = (best_bid + best_ask) / 2 if bids or asks else 0.0

                    # Find which position this token_id belongs to
                    direction = None
                    for p in self._positions:
                        if p.token_id == token_id:
                            direction = p.direction
                            break

                    if direction is None:
                        continue

                    if mid >= 0.90:
                        log.info("Resolution (CLOB book): %s → %s (mid=%.3f token=%s…)",
                                 slug, direction, mid, token_id[:8])
                        return WindowResult(slug=slug, direction=direction,
                                            resolved_at=time.time())
                    elif mid <= 0.10:
                        # This token lost — winner is the opposite direction
                        winner = "DOWN" if direction == "UP" else "UP"
                        log.info("Resolution (CLOB book loser): %s → %s (mid=%.3f token=%s…)",
                                 slug, winner, mid, token_id[:8])
                        return WindowResult(slug=slug, direction=winner,
                                            resolved_at=time.time())
                    else:
                        log.debug("CLOB: %s token %s… mid=%.3f — not yet settled",
                                  slug, token_id[:8], mid)
            except Exception as e:
                log.debug("CLOB resolution error for token %s: %s", token_id[:8], e)
        return None

    def _settle_window(self, slug: str, result: WindowResult):
        for position in self._positions:
            if position.window_slug == slug and not position.closed:
                self._settle(position, result)

    def _settle(self, position: Position, result: WindowResult):
        position.closed = True

        if result.direction == "PUSH":
            # Force-expiry fallback: treat as a break-even push.
            position.outcome = "PUSH"
            position.exit_price = position.entry_price
            position.realized_pnl = 0.0
        elif position.direction == result.direction:
            position.outcome = "WIN"
            position.exit_price = 1.0
            position.realized_pnl = position.win_pnl()
        else:
            position.outcome = "LOSS"
            position.exit_price = 0.0
            position.realized_pnl = position.loss_pnl()

        self.total_realized_pnl += position.realized_pnl
        log.info("Settled %s/%s: %s | PnL=%.4f USDC",
                 position.strategy, position.window_slug,
                 position.outcome, position.realized_pnl)

        for cb in self._pnl_callbacks:
            asyncio.create_task(cb(position, position.realized_pnl))

    def _current_daily_pnl(self) -> float:
        today_start = self._start_of_day()
        return sum(
            p.realized_pnl for p in self._positions
            if p.closed and p.opened_at >= today_start
        )

    def _reset_daily_pnl_if_needed(self):
        today = self._start_of_day()
        if today > self._day_start:
            self._day_start = today

    @staticmethod
    def _start_of_day() -> float:
        import datetime
        now = datetime.datetime.utcnow()
        sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return sod.timestamp()

    def stop(self):
        self._running = False
