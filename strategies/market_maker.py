"""
Strategy #5: Adaptive Market Making

Posts two-sided quotes on UP and DOWN tokens.
Profit comes from spread capture, not directional prediction.
2026 meta: maker orders have zero fees + rebates.

Adjustments:
- Time decay: wider spread early in window, tighter near end
- Volatility: wider spread when BTC is moving fast
- Inventory skew: lean quotes if holding too much of one side
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from config import sigmoid
from strategies.base_strategy import BaseStrategy, MarketState, Signal
from core.order_manager import OrderManager

log = logging.getLogger(__name__)

BASE_SPREAD = config.MM_BASE_SPREAD      # 0.03
REFRESH_SECS = config.MM_REFRESH_SECS   # 5.0
MAX_INVENTORY = config.MM_MAX_INVENTORY  # $20 per side


@dataclass
class MMQuotes:
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float
    fair_up: float


@dataclass
class MMInventory:
    usdc_up: float = 0.0      # USDC worth of UP tokens held
    usdc_down: float = 0.0    # USDC worth of DOWN tokens held


class MarketMaker(BaseStrategy):
    NAME = "market_maker"

    def __init__(self):
        super().__init__()
        self._inventory = MMInventory()
        self._last_refresh: float = 0.0
        self._active_orders: dict[str, str] = {}   # side -> order_id
        self._current_window: str = ""

    async def analyze(self, state: MarketState) -> Signal:
        # Don't market-make in last 30s (adverse selection risk)
        if state.seconds_remaining < 30:
            return Signal(reason="Too close to window end for MM")

        now = time.time()
        if now - self._last_refresh < REFRESH_SECS:
            return Signal(reason="Waiting for refresh interval")

        self._last_refresh = now

        # Compute fair value
        delta = state.window_delta
        fair_up = 0.50 + sigmoid(delta * 1000) * 0.50
        fair_down = 1.0 - fair_up

        # Compute spread
        spread = self._compute_spread(state)

        # Inventory skew: if over-exposed on one side, lean quotes
        skew = self._compute_skew()

        quotes = MMQuotes(
            up_bid=max(0.01, fair_up - spread / 2 + skew),
            up_ask=min(0.99, fair_up + spread / 2 + skew),
            down_bid=max(0.01, fair_down - spread / 2 - skew),
            down_ask=min(0.99, fair_down + spread / 2 - skew),
            fair_up=fair_up,
        )

        log.debug("MM quotes: UP [%.3f/%.3f] DOWN [%.3f/%.3f] spread=%.4f",
                  quotes.up_bid, quotes.up_ask, quotes.down_bid, quotes.down_ask, spread)

        # Return a special NEUTRAL signal carrying quote info in reason
        return Signal(
            direction="NEUTRAL",
            confidence=0.0,
            suggested_price=quotes.up_ask,
            suggested_size=0.0,
            reason=f"MM|UP_BID={quotes.up_bid:.4f}|UP_ASK={quotes.up_ask:.4f}"
                   f"|DN_BID={quotes.down_bid:.4f}|DN_ASK={quotes.down_ask:.4f}"
                   f"|FAIR={fair_up:.4f}",
        )

    def should_trade(self, signal: Signal, state: MarketState) -> bool:
        # MM always wants to place quotes unless at inventory limits
        if self._inventory.usdc_up >= MAX_INVENTORY and self._inventory.usdc_down >= MAX_INVENTORY:
            return False
        return "MM|" in signal.reason

    def parse_quotes(self, signal: Signal) -> Optional[MMQuotes]:
        """Extract quote levels from the signal reason string."""
        try:
            parts = dict(p.split("=") for p in signal.reason.split("|")[1:])
            return MMQuotes(
                up_bid=float(parts["UP_BID"]),
                up_ask=float(parts["UP_ASK"]),
                down_bid=float(parts["DN_BID"]),
                down_ask=float(parts["DN_ASK"]),
                fair_up=float(parts["FAIR"]),
            )
        except Exception:
            return None

    def record_fill(self, direction: str, price: float, size_usdc: float, is_buy: bool):
        """Update inventory when a maker order fills."""
        if direction == "UP":
            if is_buy:
                self._inventory.usdc_up += size_usdc
            else:
                self._inventory.usdc_up = max(0, self._inventory.usdc_up - size_usdc)
        else:
            if is_buy:
                self._inventory.usdc_down += size_usdc
            else:
                self._inventory.usdc_down = max(0, self._inventory.usdc_down - size_usdc)

    def clear_window(self):
        """Reset at window boundary."""
        self._inventory = MMInventory()
        self._active_orders.clear()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _compute_spread(self, state: MarketState) -> float:
        """Wider spread when volatile, tighter near window end."""
        # Volatility component: use momentum as proxy
        vol_mult = 1.0 + abs(state.momentum_30s) * 500
        # Time decay: spread narrows as window closes (less adverse selection)
        time_frac = state.time_fraction()
        time_decay = max(0.4, 1.0 - time_frac * 0.6)
        return BASE_SPREAD * vol_mult * time_decay

    def _compute_skew(self) -> float:
        """Positive skew = lean UP bids higher (sell UP, buy DOWN to rebalance)."""
        imbalance = self._inventory.usdc_up - self._inventory.usdc_down
        return imbalance / MAX_INVENTORY * 0.02  # max 2 cent skew
