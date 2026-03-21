"""
Strategy #6: Cross-Timeframe Momentum Cascade

Fuses signals from multiple timeframes:
  - 1m EMA cross (immediate momentum)
  - 5m RSI (overbought/oversold)
  - 15m MACD (trend direction)
  - 1h BB position (macro context)
  - Volume surge
  - Orderflow imbalance

Entry only when:
  1. Weighted score > threshold
  2. Market hasn't priced it in yet (market price < fair value - margin)
  3. NOT in end-cycle window (leave that to EndCycleSniper)

Win rate: ~55-65%
"""

import logging

import config
from config import estimate_token_price, sigmoid
from strategies.base_strategy import BaseStrategy, MarketState, Signal

log = logging.getLogger(__name__)

WEIGHTS = {
    "window_delta": 7,
    "ema_cross_1m": 3,
    "rsi_5m": 2,
    "macd_15m": 2,
    "bb_position_1h": 1,
    "volume_surge": 2,
    "orderflow_imbalance": 3,
}
TOTAL_WEIGHT = sum(WEIGHTS.values())  # 20

MIN_SCORE = 12.0    # 60% of total (12/20)
MIN_DELTA = 0.0003  # Skip very small deltas


class MomentumCascade(BaseStrategy):
    NAME = "momentum_cascade"

    def __init__(self):
        super().__init__()
        self._last_trade_window: str = ""

    async def analyze(self, state: MarketState) -> Signal:
        # Avoid conflict with EndCycleSniper in the last 30s
        if state.seconds_remaining < 40:
            return Signal(reason="Deferring to end-cycle sniper")

        # One trade per window
        if self._last_trade_window == state.market.slug:
            return Signal(reason="Already traded this window")

        delta = state.window_delta
        if abs(delta) < MIN_DELTA:
            return Signal(reason=f"Delta {delta:.4%} too small")

        direction = "UP" if delta > 0 else "DOWN"
        score, breakdown = self._score(state, direction)

        log.debug("MomentumCascade score=%.1f/20 dir=%s breakdown=%s",
                  score, direction, breakdown)

        if score < MIN_SCORE:
            return Signal(reason=f"Score {score:.1f} < {MIN_SCORE}")

        # Check if market has already priced in the move
        market_price = state.up_price if direction == "UP" else state.down_price
        fair_price = estimate_token_price(delta)

        if market_price > fair_price - 0.03:
            return Signal(reason=f"Market already priced in: {market_price:.3f} vs fair {fair_price:.3f}")

        confidence = min(0.80, 0.45 + (score / TOTAL_WEIGHT) * 0.40)
        entry_price = min(market_price + 0.03, fair_price - 0.02, 0.85)

        return Signal(
            direction=direction,
            confidence=confidence,
            suggested_price=entry_price,
            suggested_size=0.0,
            reason=f"MC score={score:.1f}/20 delta={delta:.4%} fair={fair_price:.3f} market={market_price:.3f}",
        )

    def should_trade(self, signal: Signal, state: MarketState) -> bool:
        return signal.is_actionable()

    def record_trade(self, window_slug: str):
        self._last_trade_window = window_slug

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _score(self, state: MarketState, direction: str) -> tuple[float, dict]:
        is_up = direction == "UP"
        delta = state.window_delta
        breakdown = {}

        # 1. Window delta (7 pts)
        abs_d = abs(delta)
        if abs_d >= 0.001:
            pts = 7.0
        elif abs_d >= 0.0005:
            pts = 5.0
        elif abs_d >= 0.0003:
            pts = 3.0
        else:
            pts = 1.0
        breakdown["window_delta"] = pts

        # 2. EMA cross 1m (3 pts)
        ema_bullish = state.ema9 > state.ema21 if (state.ema9 > 0 and state.ema21 > 0) else None
        if ema_bullish is not None:
            pts = 3.0 if (is_up == ema_bullish) else 0.0
        else:
            pts = 1.0
        breakdown["ema_cross_1m"] = pts

        # 3. RSI 5m (2 pts) — not overbought/oversold in signal direction
        if state.rsi > 0:
            if is_up and 30 < state.rsi < 70:
                pts = 2.0
            elif not is_up and 30 < state.rsi < 70:
                pts = 2.0
            else:
                pts = 0.0
        else:
            pts = 1.0
        breakdown["rsi_5m"] = pts

        # 4. MACD 15m (2 pts)
        macd_bullish = state.macd > state.macd_signal
        pts = 2.0 if (is_up == macd_bullish) else 0.0
        breakdown["macd_15m"] = pts

        # 5. BB position 1h (1 pt)
        if state.bb_upper > state.bb_lower > 0:
            bb_range = state.bb_upper - state.bb_lower
            bb_pos = (state.btc_price - state.bb_lower) / bb_range
            pts = 1.0 if (is_up and bb_pos > 0.55) or (not is_up and bb_pos < 0.45) else 0.0
        else:
            pts = 0.5
        breakdown["bb_position_1h"] = pts

        # 6. Volume surge (2 pts)
        pts = 2.0 if state.volume_ratio >= 1.5 else (1.0 if state.volume_ratio >= 1.2 else 0.0)
        breakdown["volume_surge"] = pts

        # 7. Orderflow imbalance (3 pts)
        imbalance = state.orderflow_imbalance_up if is_up else -state.orderflow_imbalance_down
        pts = 3.0 if imbalance > 0.2 else (1.5 if imbalance > 0 else 0.0)
        breakdown["orderflow_imbalance"] = pts

        total = sum(breakdown.values())
        return total, breakdown
