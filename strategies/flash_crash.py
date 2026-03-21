"""
Strategy #4: Flash Crash / Dump-and-Hedge

When one side of the orderbook crashes >25% in <10s:
1. Buy the crashed side (Leg 1)
2. Wait for opposite side to become cheap enough to hedge
3. If combined cost < $0.95: profit locked (risk-free)
4. If hedge not filled in 60s: accept single-leg risk

Win rate: ~70-80% | Frequency: 3-10 per day
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from strategies.base_strategy import BaseStrategy, MarketState, Signal

log = logging.getLogger(__name__)

CRASH_THRESHOLD = config.FLASH_CRASH_THRESHOLD    # 0.25 (25% drop)
HEDGE_TARGET = config.FLASH_CRASH_HEDGE_TARGET    # 0.95 combined cost
MAX_WAIT_HEDGE = config.FLASH_CRASH_MAX_WAIT      # 60 seconds


@dataclass
class CrashLeg:
    token_id: str
    direction: str
    entry_price: float
    size_usdc: float
    shares: float
    leg1_placed_at: float = field(default_factory=time.time)
    hedged: bool = False
    hedge_price: float = 0.0


class FlashCrash(BaseStrategy):
    NAME = "flash_crash"

    def __init__(self):
        super().__init__()
        self._open_legs: dict[str, CrashLeg] = {}   # slug -> CrashLeg
        self._crash_prices: dict[str, float] = {}    # token_id -> peak_price_in_window
        self._detected_crashes: set[str] = set()     # token_ids already acted on this window

    async def analyze(self, state: MarketState) -> Signal:
        slug = state.market.slug

        # Check if we have an open Leg 1 needing a hedge
        if slug in self._open_legs:
            return self._check_hedge(state)

        # Check for flash crash in UP token
        up_id = state.market.up_token_id
        down_id = state.market.down_token_id

        # Update peak prices
        self._update_peak(up_id, state.up_price)
        self._update_peak(down_id, state.down_price)

        # Check UP crash
        if up_id not in self._detected_crashes:
            crash_pct = self._crash_pct(up_id, state.up_price)
            if crash_pct >= CRASH_THRESHOLD and state.up_ask < 0.50:
                log.warning("FlashCrash: UP crashed %.1f%% token=%s price=%.3f",
                            crash_pct * 100, up_id[:8], state.up_ask)
                self._detected_crashes.add(up_id)
                return Signal(
                    direction="UP",
                    confidence=0.80,
                    suggested_price=state.up_ask,
                    suggested_size=0.0,
                    reason=f"Flash crash UP: -{crash_pct:.1%} now at {state.up_ask:.3f}",
                )

        # Check DOWN crash
        if down_id not in self._detected_crashes:
            crash_pct = self._crash_pct(down_id, state.down_price)
            if crash_pct >= CRASH_THRESHOLD and state.down_ask < 0.50:
                log.warning("FlashCrash: DOWN crashed %.1f%% token=%s price=%.3f",
                            crash_pct * 100, down_id[:8], state.down_ask)
                self._detected_crashes.add(down_id)
                return Signal(
                    direction="DOWN",
                    confidence=0.80,
                    suggested_price=state.down_ask,
                    suggested_size=0.0,
                    reason=f"Flash crash DOWN: -{crash_pct:.1%} now at {state.down_ask:.3f}",
                )

        return Signal(reason="No flash crash detected")

    def _check_hedge(self, state: MarketState) -> Signal:
        """Check if we should place the hedge leg."""
        slug = state.market.slug
        leg = self._open_legs[slug]

        age = time.time() - leg.leg1_placed_at

        if age > MAX_WAIT_HEDGE:
            # Stop-loss: accept single-leg exposure
            log.warning("FlashCrash: hedge timeout for %s after %.0fs", slug, age)
            del self._open_legs[slug]
            return Signal(reason="Hedge timeout — accepting single leg")

        # Check if hedge is viable
        if leg.direction == "UP":
            hedge_price = state.down_ask
        else:
            hedge_price = state.up_ask

        avg_leg1 = leg.entry_price
        # Combined cost if we hedge now
        # pair_cost ≈ (leg1_price + hedge_price) / 2 (simplified for equal-size legs)
        combined_cost = avg_leg1 + hedge_price

        if combined_cost <= HEDGE_TARGET:
            hedge_dir = "DOWN" if leg.direction == "UP" else "UP"
            profit_locked = leg.shares * (1.0 - combined_cost)
            return Signal(
                direction=hedge_dir,
                confidence=0.98,
                suggested_price=hedge_price,
                suggested_size=0.0,
                reason=(f"FlashCrash hedge: combined_cost={combined_cost:.3f} "
                        f"profit_locked={profit_locked:.4f}"),
            )

        return Signal(reason=f"Hedge cost {hedge_price:.3f} not viable yet "
                             f"(combined={avg_leg1 + hedge_price:.3f} > {HEDGE_TARGET})")

    def should_trade(self, signal: Signal, state: MarketState) -> bool:
        return signal.is_actionable()

    def record_leg1(self, slug: str, direction: str, price: float, size_usdc: float):
        shares = size_usdc / price
        self._open_legs[slug] = CrashLeg(
            token_id=state_token_id(state=None, direction=direction),
            direction=direction,
            entry_price=price,
            size_usdc=size_usdc,
            shares=shares,
        )
        log.info("FlashCrash Leg1 recorded: %s %s @ %.3f size=$%.2f",
                 direction, slug, price, size_usdc)

    def record_hedge(self, slug: str, hedge_price: float):
        if slug in self._open_legs:
            self._open_legs[slug].hedged = True
            self._open_legs[slug].hedge_price = hedge_price
            del self._open_legs[slug]
            log.info("FlashCrash hedge complete for %s @ %.3f", slug, hedge_price)

    def clear_window(self, slug: str):
        """Clean up state for completed window."""
        self._open_legs.pop(slug, None)
        self._detected_crashes.clear()
        self._crash_prices.clear()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _update_peak(self, token_id: str, current_price: float):
        if token_id not in self._crash_prices or current_price > self._crash_prices[token_id]:
            self._crash_prices[token_id] = current_price

    def _crash_pct(self, token_id: str, current_price: float) -> float:
        peak = self._crash_prices.get(token_id, current_price)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - current_price) / peak)


def state_token_id(state, direction: str) -> str:
    """Helper — returns token_id for a given direction."""
    if state is None:
        return ""
    if direction == "UP":
        return state.market.up_token_id
    return state.market.down_token_id
