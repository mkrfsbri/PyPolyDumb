"""
Strategy #3: Latency Arbitrage (Spot Momentum Lag)

Detects when Binance spot has moved significantly but Polymarket
hasn't priced it in yet. Places maker order AHEAD of market catch-up.

Win rate: ~80-90% when edge > 8%
"""

import logging
import math
import time

import config
from config import sigmoid
from strategies.base_strategy import BaseStrategy, MarketState, Signal

log = logging.getLogger(__name__)

MIN_EDGE = config.LATENCY_MIN_EDGE        # 0.08
CANCEL_AFTER = config.LATENCY_CANCEL_SECS  # 15s
MOMENTUM_THRESHOLD = 0.0005               # 0.05% in 30s


class LatencyArb(BaseStrategy):
    NAME = "latency_arb"

    def __init__(self):
        super().__init__()
        self._last_trade_window: str = ""

    async def analyze(self, state: MarketState) -> Signal:
        # One trade per window maximum
        if self._last_trade_window == state.market.slug:
            return Signal(reason="Already traded this window")

        # Need enough time remaining (don't arb in last 10s)
        if state.seconds_remaining < 15:
            return Signal(reason="Too close to window end")

        delta = state.window_delta
        momentum = state.momentum_30s

        # Spot-implied probability: sigmoid(0)=0.5 naturally, no offset needed
        spot_implied_up = sigmoid(delta * 200)

        # Current market implied from orderbook
        market_implied_up = state.up_price

        # Edge: difference between what spot says vs what market prices
        edge_up = spot_implied_up - market_implied_up
        edge_down = (1.0 - spot_implied_up) - state.down_price

        # Check momentum confirmation
        if abs(momentum) < MOMENTUM_THRESHOLD:
            return Signal(reason=f"Momentum {momentum:.5f} below threshold")

        if edge_up >= MIN_EDGE and momentum > 0:
            # Market underpricing UP — buy UP
            entry_price = min(market_implied_up + 0.02, 0.85)
            return Signal(
                direction="UP",
                confidence=min(0.90, 0.50 + edge_up),
                suggested_price=entry_price,
                suggested_size=0.0,
                reason=(f"Latency arb UP: spot_implied={spot_implied_up:.3f} "
                        f"market={market_implied_up:.3f} edge={edge_up:.3f}"),
            )

        if edge_down >= MIN_EDGE and momentum < 0:
            # Market underpricing DOWN — buy DOWN
            entry_price = min(state.down_price + 0.02, 0.85)
            return Signal(
                direction="DOWN",
                confidence=min(0.90, 0.50 + edge_down),
                suggested_price=entry_price,
                suggested_size=0.0,
                reason=(f"Latency arb DOWN: spot_implied={1-spot_implied_up:.3f} "
                        f"market={state.down_price:.3f} edge={edge_down:.3f}"),
            )

        return Signal(reason=f"No arb edge (up_edge={edge_up:.3f}, dn_edge={edge_down:.3f})")

    def should_trade(self, signal: Signal, state: MarketState) -> bool:
        return signal.is_actionable()

    def record_trade(self, window_slug: str):
        self._last_trade_window = window_slug
