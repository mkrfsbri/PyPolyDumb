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
    # 120s gives the Gamma API ~12 polling cycles. The position limit is now
    # scoped per-window so this no longer blocks fresh-window trading.
    _PUSH_EXPIRE_GRACE = 120

    async def _check_resolutions(self):
        """Check Gamma API for resolved markets with open positions."""
        pending_slugs = {p.window_slug for p in self._positions
                         if not p.closed and p.window_slug not in self._resolved_windows}
        if not pending_slugs:
            return

        async with aiohttp.ClientSession() as session:
            for slug in pending_slugs:
                try:
                    result = await self._fetch_resolution(slug, session)
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
        self, slug: str, session: aiohttp.ClientSession
    ) -> Optional[WindowResult]:
        url = f"{config.GAMMA_API}/markets"
        params = {"slug": slug}
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not data:
                    return None
                market = data[0] if isinstance(data, list) else data

                tokens = market.get("tokens", [])
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)

                # Primary: API has set the resolved flag
                resolved = market.get("resolved") or market.get("is_resolved") or False
                if resolved:
                    for token in tokens:
                        if float(token.get("winner_payout") or 0) == 1.0:
                            outcome = (token.get("outcome") or "").upper()
                            direction = "UP" if "UP" in outcome or outcome == "YES" else "DOWN"
                            return WindowResult(
                                slug=slug,
                                direction=direction,
                                resolved_at=time.time(),
                            )

                # Fallback: market is closed and one token price has settled to 1.0.
                # This fires before the resolved flag is set.
                active = market.get("active", True)
                closed = market.get("closed", False)
                if not active or closed:
                    # Shape A: tokens list with price field
                    for token in tokens:
                        price = float(token.get("price") or 0)
                        if price >= 0.99:
                            outcome = (token.get("outcome") or "").upper()
                            direction = "UP" if "UP" in outcome or outcome == "YES" else "DOWN"
                            log.debug("Resolution via token price: %s → %s", slug, direction)
                            return WindowResult(
                                slug=slug,
                                direction=direction,
                                resolved_at=time.time(),
                            )

                    # Shape B: outcomePrices / outcomes / clobTokenIds (all JSON strings)
                    raw_prices = market.get("outcomePrices")
                    raw_outcomes = market.get("outcomes")
                    if raw_prices and raw_outcomes:
                        if isinstance(raw_prices, str):
                            raw_prices = json.loads(raw_prices)
                        if isinstance(raw_outcomes, str):
                            raw_outcomes = json.loads(raw_outcomes)
                        for outcome_label, price_str in zip(raw_outcomes, raw_prices):
                            if float(price_str) >= 0.99:
                                outcome = str(outcome_label).upper()
                                direction = "UP" if "UP" in outcome or outcome == "YES" else "DOWN"
                                log.debug("Resolution via outcomePrices: %s → %s", slug, direction)
                                return WindowResult(
                                    slug=slug,
                                    direction=direction,
                                    resolved_at=time.time(),
                                )
        except Exception:
            pass
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
