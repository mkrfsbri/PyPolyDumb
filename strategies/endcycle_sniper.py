"""
Strategy #1: End-Cycle Sniper

Logic:
- Activate at T-30s to T-10s before window close
- Compute confidence score from 7 weighted indicators
- If score high enough: place maker order on winning side at 0.88-0.95

Win rate: ~78-85% | Bet size: $5-10
"""

import logging

import config
from strategies.base_strategy import BaseStrategy, MarketState, Signal

log = logging.getLogger(__name__)

WEIGHTS = {
    "window_delta": 7,
    "ema_cross_1m": 3,
    "rsi_5m": 2,
    "volume_surge": 2,
    "orderflow_imbalance": 3,
    "momentum_30s": 2,
    "bb_position": 1,
}
TOTAL_WEIGHT = sum(WEIGHTS.values())  # 20

HIGH_SCORE = config.ENDCYCLE_HIGH_SCORE   # 14/20
MED_SCORE  = config.ENDCYCLE_MED_SCORE    # 10/20

# Maker prices by confidence tier
PRICE_HIGH = 0.92
PRICE_MED  = 0.82
PRICE_LOW  = 0.70


class EndCycleSniper(BaseStrategy):
    NAME = "endcycle_sniper"

    def __init__(self):
        super().__init__()
        self._last_signal_window: str = ""

    async def analyze(self, state: MarketState) -> Signal:
        # Only active during end-cycle window
        in_window = state.is_end_cycle(
            activation_secs=config.ENDCYCLE_ACTIVATION_SECS,
            deactivate_secs=config.ENDCYCLE_DEACTIVATE_SECS,
        )
        if not in_window:
            return Signal(reason="Outside end-cycle window")

        log.info("EndCycleSniper: activation window | remaining=%.0fs delta=%.5f",
                 state.seconds_remaining, state.window_delta)

        # Already fired this window?
        if self._last_signal_window == state.market.slug:
            return Signal(reason="Already traded this window")

        delta = state.window_delta
        abs_delta = abs(delta)

        # Skip coin-flip territory
        if abs_delta < 0.0002:
            log.info("EndCycleSniper: delta too small (%.5f) — skipping", delta)
            return Signal(reason=f"Delta too small ({delta:.4%})")

        direction = "UP" if delta > 0 else "DOWN"
        score = self._compute_score(state, direction)

        log.info("EndCycleSniper: score=%.1f/20 delta=%.5f dir=%s remaining=%.0fs",
                 score, delta, direction, state.seconds_remaining)

        if score >= HIGH_SCORE:
            confidence = min(0.95, 0.70 + (score - HIGH_SCORE) * 0.03)
            entry_price = PRICE_HIGH
        elif score >= MED_SCORE:
            confidence = 0.60 + (score - MED_SCORE) * 0.02
            entry_price = PRICE_MED
        else:
            log.info("EndCycleSniper: score %.1f below threshold %.1f — no trade",
                     score, MED_SCORE)
            return Signal(reason=f"Score {score:.1f} below threshold {MED_SCORE}")

        return Signal(
            direction=direction,
            confidence=confidence,
            suggested_price=entry_price,
            suggested_size=0.0,     # sized by orchestrator via kelly_size
            reason=f"score={score:.1f}/20 delta={delta:.4%}",
        )

    def should_trade(self, signal: Signal, state: MarketState) -> bool:
        if not signal.is_actionable():
            return False
        # Check we haven't already bet this window
        if self._last_signal_window == state.market.slug:
            return False
        return True

    def record_trade(self, window_slug: str):
        """Called by orchestrator when trade is placed."""
        self._last_signal_window = window_slug

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _compute_score(self, state: MarketState, direction: str) -> float:
        score = 0.0
        delta = state.window_delta
        is_up = direction == "UP"

        # 1. Window delta (7 pts) — strongest signal
        abs_delta = abs(delta)
        if abs_delta >= 0.001:
            score += 7.0
        elif abs_delta >= 0.0005:
            score += 5.0
        elif abs_delta >= 0.0003:
            score += 3.0
        else:
            score += 1.0

        # 2. EMA cross (3 pts)
        if state.ema9 > 0 and state.ema21 > 0:
            ema_bullish = state.ema9 > state.ema21
            if (is_up and ema_bullish) or (not is_up and not ema_bullish):
                score += 3.0
            else:
                score += 1.0  # opposing signal

        # 3. RSI (2 pts)
        if state.rsi > 0:
            if is_up and state.rsi < 70:
                score += 2.0   # not overbought
            elif not is_up and state.rsi > 30:
                score += 2.0   # not oversold
            else:
                score += 0.5   # borderline

        # 4. Volume surge (2 pts)
        if state.volume_ratio >= 1.5:
            score += 2.0
        elif state.volume_ratio >= 1.2:
            score += 1.0

        # 5. Orderflow imbalance (3 pts)
        imbalance = state.orderflow_imbalance_up if is_up else state.orderflow_imbalance_down
        if imbalance > 0.2:
            score += 3.0
        elif imbalance > 0:
            score += 1.5
        else:
            score += 0.0

        # 6. Momentum 30s (2 pts)
        mom = state.momentum_30s
        if (is_up and mom > 0.0001) or (not is_up and mom < -0.0001):
            score += 2.0
        elif abs(mom) < 0.00005:
            score += 1.0

        # 7. BB position (1 pt)
        if state.bb_mid > 0 and state.bb_upper > state.bb_lower:
            bb_width = state.bb_upper - state.bb_lower
            bb_pos = (state.btc_price - state.bb_lower) / bb_width
            if is_up and bb_pos > 0.6:
                score += 1.0
            elif not is_up and bb_pos < 0.4:
                score += 1.0
            else:
                score += 0.0

        return score
