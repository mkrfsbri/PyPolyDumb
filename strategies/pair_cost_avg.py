"""
Strategy #2: Pair Cost Averaging (Gabagool Strategy)

Market-neutral — doesn't predict direction.
Buys UP and DOWN tokens asynchronously when either side is cheap.
Profit is guaranteed if total pair cost < $1.00.

pair_cost = (spent_UP + spent_DOWN) / min(qty_UP, qty_DOWN)
profit    = min(qty_UP, qty_DOWN) × (1.00 - pair_cost)

Win rate: ~95-98%
"""

import logging
from dataclasses import dataclass, field

import config
from strategies.base_strategy import BaseStrategy, MarketState, Signal

log = logging.getLogger(__name__)

TRIGGER_PRICE = config.PAIR_COST_TRIGGER   # Buy side when price < $0.35
MAX_PAIR_COST = config.PAIR_COST_MAX        # Only hedge if pair_cost < $0.97
MAX_LEGS_PER_SIDE = 3                       # Max top-ups per side per window


@dataclass
class PairState:
    """Per-window state for the Gabagool strategy."""
    slug: str
    spent_up: float = 0.0
    spent_down: float = 0.0
    qty_up: float = 0.0
    qty_down: float = 0.0
    legs_up: int = 0
    legs_down: int = 0
    completed: bool = False

    def pair_cost(self) -> float:
        matched_qty = min(self.qty_up, self.qty_down)
        if matched_qty <= 0:
            return 99.0
        return (self.spent_up + self.spent_down) / matched_qty

    def potential_profit(self) -> float:
        matched_qty = min(self.qty_up, self.qty_down)
        cost = self.pair_cost()
        if cost >= 1.0:
            return 0.0
        return matched_qty * (1.0 - cost)

    def avg_up_cost(self) -> float:
        return self.spent_up / self.qty_up if self.qty_up > 0 else 0.0

    def avg_down_cost(self) -> float:
        return self.spent_down / self.qty_down if self.qty_down > 0 else 0.0


class PairCostAvg(BaseStrategy):
    NAME = "pair_cost_avg"

    def __init__(self):
        super().__init__()
        self._windows: dict[str, PairState] = {}

    def _get_state(self, slug: str) -> PairState:
        if slug not in self._windows:
            self._windows[slug] = PairState(slug=slug)
        return self._windows[slug]

    async def analyze(self, state: MarketState) -> Signal:
        pair = self._get_state(state.market.slug)

        if pair.completed:
            return Signal(reason="Pair already completed this window")

        up_ask = state.up_ask
        down_ask = state.down_ask

        # Check if we should buy UP leg
        if (up_ask <= TRIGGER_PRICE
                and pair.legs_up < MAX_LEGS_PER_SIDE
                and not self._would_exceed_pair_cost(pair, "UP", up_ask, state)):
            return Signal(
                direction="UP",
                confidence=0.97,
                suggested_price=up_ask,
                suggested_size=0.0,
                reason=f"UP cheap at {up_ask:.3f} | pair_cost would be "
                       f"{self._projected_cost(pair, 'UP', up_ask):.3f}",
            )

        # Check if we should buy DOWN leg
        if (down_ask <= TRIGGER_PRICE
                and pair.legs_down < MAX_LEGS_PER_SIDE
                and not self._would_exceed_pair_cost(pair, "DOWN", down_ask, state)):
            return Signal(
                direction="DOWN",
                confidence=0.97,
                suggested_price=down_ask,
                suggested_size=0.0,
                reason=f"DOWN cheap at {down_ask:.3f} | pair_cost would be "
                       f"{self._projected_cost(pair, 'DOWN', down_ask):.3f}",
            )

        # Check if we should COMPLETE the pair (one side very cheap as hedge)
        if pair.qty_up > 0 and pair.qty_down == 0 and down_ask < 0.50:
            projected = self._projected_cost(pair, "DOWN", down_ask)
            if projected < MAX_PAIR_COST:
                return Signal(
                    direction="DOWN",
                    confidence=0.95,
                    suggested_price=down_ask,
                    suggested_size=0.0,
                    reason=f"Completing pair: DOWN hedge at {down_ask:.3f} "
                           f"(projected pair_cost={projected:.3f})",
                )

        if pair.qty_down > 0 and pair.qty_up == 0 and up_ask < 0.50:
            projected = self._projected_cost(pair, "UP", up_ask)
            if projected < MAX_PAIR_COST:
                return Signal(
                    direction="UP",
                    confidence=0.95,
                    suggested_price=up_ask,
                    suggested_size=0.0,
                    reason=f"Completing pair: UP hedge at {up_ask:.3f} "
                           f"(projected pair_cost={projected:.3f})",
                )

        return Signal(reason="No pair cost opportunity")

    def should_trade(self, signal: Signal, state: MarketState) -> bool:
        return signal.is_actionable()

    def record_order_placed(self, slug: str, direction: str):
        """Called immediately on order placement to block duplicate orders before fill."""
        pair = self._get_state(slug)
        if direction == "UP":
            pair.legs_up += 1
        else:
            pair.legs_down += 1
        log.debug("PairCostAvg order placed: %s %s legs_up=%d legs_down=%d",
                  direction, slug, pair.legs_up, pair.legs_down)

    def record_fill(self, slug: str, direction: str, price: float, size_usdc: float):
        """Called when an order fills — updates qty/spend. legs already counted at placement."""
        pair = self._get_state(slug)
        shares = size_usdc / price
        if direction == "UP":
            pair.spent_up += size_usdc
            pair.qty_up += shares
        else:
            pair.spent_down += size_usdc
            pair.qty_down += shares

        cost = pair.pair_cost()
        profit = pair.potential_profit()
        cost_str = "unmatched (one side only)" if cost >= 99.0 else f"{cost:.4f}"
        log.info("PairCostAvg fill: %s side at %.3f | pair_cost=%s potential_profit=%.4f",
                 direction, price, cost_str, profit)

        if pair.qty_up > 0 and pair.qty_down > 0 and cost < MAX_PAIR_COST:
            pair.completed = True
            log.info("PairCostAvg: PAIR COMPLETE! slug=%s profit=%.4f USDC", slug, profit)

    def pair_summary(self, slug: str) -> dict:
        pair = self._get_state(slug)
        return {
            "slug": slug,
            "qty_up": pair.qty_up,
            "qty_down": pair.qty_down,
            "spent_up": pair.spent_up,
            "spent_down": pair.spent_down,
            "pair_cost": pair.pair_cost(),
            "potential_profit": pair.potential_profit(),
            "completed": pair.completed,
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _projected_cost(self, pair: PairState, direction: str, price: float) -> float:
        """Estimate pair cost if we buy one more leg on direction at price."""
        size = 5.0   # $5 per leg for estimation
        shares = size / price
        if direction == "UP":
            new_spent_up = pair.spent_up + size
            new_qty_up = pair.qty_up + shares
            matched = min(new_qty_up, pair.qty_down)
            if matched <= 0:
                return 99.0
            return (new_spent_up + pair.spent_down) / matched
        else:
            new_spent_down = pair.spent_down + size
            new_qty_down = pair.qty_down + shares
            matched = min(pair.qty_up, new_qty_down)
            if matched <= 0:
                return 99.0
            return (pair.spent_up + new_spent_down) / matched

    def _would_exceed_pair_cost(
        self, pair: PairState, direction: str, price: float, state: MarketState
    ) -> bool:
        # If there's no matching side yet, allow the first leg freely.
        # The pair cost check only applies once BOTH sides have holdings.
        if direction == "UP" and pair.qty_down == 0:
            return False
        if direction == "DOWN" and pair.qty_up == 0:
            return False
        projected = self._projected_cost(pair, direction, price)
        return projected >= MAX_PAIR_COST
