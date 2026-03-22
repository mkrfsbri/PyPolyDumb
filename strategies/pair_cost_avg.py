"""
Strategy #2: Pair Cost Averaging (Gabagool Strategy)

Market-neutral — tidak perlu prediksi arah BTC.
Masuk HANYA ketika up_ask + down_ask < MAX_PAIR_COST (profit terkunci).

Flow:
  Leg 1: Beli sisi yang lebih murah
  Leg 2: Beli sisi lainnya selama combined cost masih < MAX_PAIR_COST

pair_cost = leg1_price + leg2_price
profit    = shares × (1.00 - pair_cost)

Win rate: ~95-98%
"""

import logging
from dataclasses import dataclass, field

import config
from strategies.base_strategy import BaseStrategy, MarketState, Signal

log = logging.getLogger(__name__)

MAX_PAIR_COST = config.PAIR_COST_MAX   # 0.97


@dataclass
class PairState:
    """Per-window state."""
    slug: str
    spent_up: float = 0.0
    spent_down: float = 0.0
    qty_up: float = 0.0
    qty_down: float = 0.0
    legs_up: int = 0       # max 1
    legs_down: int = 0     # max 1
    leg1_price: float = 0.0
    leg1_direction: str = ""
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
            return Signal(reason="Pair sudah complete window ini")

        # Tidak masuk di 30s terakhir — tidak ada waktu untuk leg 2
        if state.seconds_remaining < 30:
            return Signal(reason="Terlalu dekat window end")

        up_ask = state.up_ask
        down_ask = state.down_ask

        # ── Leg 1: Belum ada posisi — masuk hanya kalau pair profitable ────────
        if pair.legs_up == 0 and pair.legs_down == 0:
            combined = up_ask + down_ask
            if combined >= MAX_PAIR_COST:
                return Signal(
                    reason=f"Pair cost {combined:.3f} ≥ {MAX_PAIR_COST} — tidak ada edge"
                )
            # Beli sisi yang lebih murah duluan
            if up_ask <= down_ask:
                return Signal(
                    direction="UP",
                    confidence=0.97,
                    suggested_price=up_ask,
                    suggested_size=0.0,
                    reason=f"Leg 1 UP @ {up_ask:.3f} | combined={combined:.3f}",
                )
            else:
                return Signal(
                    direction="DOWN",
                    confidence=0.97,
                    suggested_price=down_ask,
                    suggested_size=0.0,
                    reason=f"Leg 1 DOWN @ {down_ask:.3f} | combined={combined:.3f}",
                )

        # ── Leg 2: Satu sisi sudah dipasang — lengkapi pair ───────────────────
        if pair.legs_up == 1 and pair.legs_down == 0:
            combined = pair.leg1_price + down_ask
            if combined < MAX_PAIR_COST:
                return Signal(
                    direction="DOWN",
                    confidence=0.97,
                    suggested_price=down_ask,
                    suggested_size=0.0,
                    reason=f"Leg 2 DOWN @ {down_ask:.3f} | combined={combined:.3f}",
                )
            return Signal(
                reason=f"Menunggu DOWN murah: leg1={pair.leg1_price:.3f} + down={down_ask:.3f}"
                       f" = {pair.leg1_price + down_ask:.3f} ≥ {MAX_PAIR_COST}"
            )

        if pair.legs_down == 1 and pair.legs_up == 0:
            combined = pair.leg1_price + up_ask
            if combined < MAX_PAIR_COST:
                return Signal(
                    direction="UP",
                    confidence=0.97,
                    suggested_price=up_ask,
                    suggested_size=0.0,
                    reason=f"Leg 2 UP @ {up_ask:.3f} | combined={combined:.3f}",
                )
            return Signal(
                reason=f"Menunggu UP murah: leg1={pair.leg1_price:.3f} + up={up_ask:.3f}"
                       f" = {pair.leg1_price + up_ask:.3f} ≥ {MAX_PAIR_COST}"
            )

        # Kedua legs sudah dipasang, tunggu fills
        return Signal(reason="Kedua legs dipasang, menunggu fills")

    def should_trade(self, signal: Signal, state: MarketState) -> bool:
        return signal.is_actionable()

    def record_order_placed(self, slug: str, direction: str, price: float):
        """Dipanggil segera saat order dikirim — mencegah duplikat sebelum fill."""
        pair = self._get_state(slug)
        if direction == "UP":
            pair.legs_up += 1
        else:
            pair.legs_down += 1
        # Simpan harga leg 1 untuk cek combined cost di leg 2
        if pair.legs_up + pair.legs_down == 1:
            pair.leg1_price = price
            pair.leg1_direction = direction
        log.info("PairCostAvg order placed: %s %s @ %.3f | legs_up=%d legs_down=%d",
                 direction, slug, price, pair.legs_up, pair.legs_down)

    def record_fill(self, slug: str, direction: str, price: float, size_usdc: float):
        """Dipanggil saat order fill — update qty/spend untuk hitung pair cost."""
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
        cost_str = "satu sisi saja" if cost >= 99.0 else f"{cost:.4f}"
        log.info("PairCostAvg fill: %s @ %.3f | pair_cost=%s potential_profit=%.4f USDC",
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
