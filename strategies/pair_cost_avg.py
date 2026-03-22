"""
Strategy #2: Pair Cost Averaging (Gabagool Strategy)

Masuk kedua sisi (UP+DOWN) di awal window, profit dari Leg 3 momentum.

Flow 3 Leg:
  Leg 1 : Beli sisi lebih murah ketika combined ≤ MAX_PAIR_COST (1.02)
  Leg 2 : Beli sisi lainnya — pair terkunci
  Leg 3 : T-15s, setelah pair complete — beli sisi yang mendekati 1.0
           (near-certain winner momentum bet)

Ekonomi:
  combined ask di binary market selalu ~1.00-1.04 (ask > mid)
  Pair cost ≈ 1.01 → spread loss ~$0.10/10 shares (small)
  Leg 3 @ 0.85 → gain = 10×(1.00-0.85) = +$1.50 jika menang

Win rate: ~85-95% (Leg 3 momentum)
"""

import logging
from dataclasses import dataclass

import config
from strategies.base_strategy import BaseStrategy, MarketState, Signal

log = logging.getLogger(__name__)

MAX_PAIR_COST   = config.PAIR_COST_MAX        # 0.97
LEG3_THRESHOLD  = config.PAIR_LEG3_THRESHOLD  # 0.85 — token price to trigger leg 3
LEG3_ACTIVATION = config.PAIR_LEG3_ACTIVATION # 15s remaining


@dataclass
class PairState:
    """Per-window state."""
    slug: str
    # Pair legs (1 per side)
    spent_up: float = 0.0
    spent_down: float = 0.0
    qty_up: float = 0.0
    qty_down: float = 0.0
    legs_up: int = 0
    legs_down: int = 0
    leg1_price: float = 0.0
    leg1_direction: str = ""
    completed: bool = False
    # Leg 3 — momentum confirmation at end-cycle
    leg3_placed: bool = False
    leg3_direction: str = ""
    leg3_price: float = 0.0
    leg3_qty: float = 0.0
    leg3_spent: float = 0.0

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
        up_ask = state.up_ask
        down_ask = state.down_ask
        secs = state.seconds_remaining

        # ── Leg 3: Pair complete + near resolution ────────────────────────────
        # Aktif di T-15s. Cek apakah salah satu token mendekati 1.0 (hampir menang).
        # Jika ya, beli sisi yang pasti profit sebagai directional momentum bet.
        if pair.completed and not pair.leg3_placed and secs <= LEG3_ACTIVATION:
            if up_ask >= LEG3_THRESHOLD:
                log.info(
                    "PairCostAvg Leg 3: UP mendekati resolusi @ %.3f (T-%.0fs)",
                    up_ask, secs,
                )
                return Signal(
                    direction="UP",
                    confidence=min(0.99, up_ask),
                    suggested_price=up_ask,
                    suggested_size=0.0,
                    reason=f"Leg 3 UP momentum @ {up_ask:.3f} — T-{secs:.0f}s",
                )
            if down_ask >= LEG3_THRESHOLD:
                log.info(
                    "PairCostAvg Leg 3: DOWN mendekati resolusi @ %.3f (T-%.0fs)",
                    down_ask, secs,
                )
                return Signal(
                    direction="DOWN",
                    confidence=min(0.99, down_ask),
                    suggested_price=down_ask,
                    suggested_size=0.0,
                    reason=f"Leg 3 DOWN momentum @ {down_ask:.3f} — T-{secs:.0f}s",
                )

        # Setelah pair complete, hanya Leg 3 yang bisa aktif
        if pair.completed:
            return Signal(reason="Pair complete — menunggu Leg 3 window (T-15s)")

        # ── Leg 1: Belum ada posisi — masuk hanya kalau pair profitable ────────
        if pair.legs_up == 0 and pair.legs_down == 0:
            # Tidak masuk di 30s terakhir — tidak ada waktu untuk leg 2
            if secs < 30:
                return Signal(reason="Terlalu dekat window end untuk Leg 1")
            combined = up_ask + down_ask
            if combined >= MAX_PAIR_COST:
                return Signal(
                    reason=f"Pair cost {combined:.3f} ≥ {MAX_PAIR_COST} — tidak ada edge"
                )
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
                reason=f"Menunggu DOWN murah: {pair.leg1_price:.3f}+{down_ask:.3f}"
                       f"={pair.leg1_price + down_ask:.3f} ≥ {MAX_PAIR_COST}"
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
                reason=f"Menunggu UP murah: {pair.leg1_price:.3f}+{up_ask:.3f}"
                       f"={pair.leg1_price + up_ask:.3f} ≥ {MAX_PAIR_COST}"
            )

        return Signal(reason="Kedua legs dipasang, menunggu fills")

    def should_trade(self, signal: Signal, state: MarketState) -> bool:
        return signal.is_actionable()

    def record_order_placed(self, slug: str, direction: str, price: float):
        """Dipanggil segera saat order dikirim — mencegah duplikat sebelum fill."""
        pair = self._get_state(slug)

        # Leg 3 jika pair sudah complete
        if pair.completed:
            pair.leg3_placed = True
            pair.leg3_direction = direction
            pair.leg3_price = price
            log.info("PairCostAvg Leg 3 placed: %s @ %.3f", direction, price)
            return

        if direction == "UP":
            pair.legs_up += 1
        else:
            pair.legs_down += 1

        # Simpan harga leg 1 untuk cek combined cost di leg 2
        if pair.legs_up + pair.legs_down == 1:
            pair.leg1_price = price
            pair.leg1_direction = direction

        log.info("PairCostAvg order placed: %s @ %.3f | legs_up=%d legs_down=%d",
                 direction, price, pair.legs_up, pair.legs_down)

    def record_fill(self, slug: str, direction: str, price: float, size_usdc: float):
        """Dipanggil saat order fill — update qty/spend."""
        pair = self._get_state(slug)
        shares = size_usdc / price

        # Leg 3 fill
        if pair.leg3_placed and pair.leg3_direction == direction and pair.leg3_qty == 0:
            pair.leg3_qty = shares
            pair.leg3_spent = size_usdc
            log.info(
                "PairCostAvg Leg 3 fill: %s @ %.3f — %.2f shares ($%.2f) | T-momentum",
                direction, price, shares, size_usdc,
            )
            return

        # Leg 1 / 2 fill
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
            log.info("PairCostAvg: PAIR COMPLETE! slug=%s profit_terkunci=%.4f USDC",
                     slug, profit)

    def pair_summary(self, slug: str) -> dict:
        pair = self._get_state(slug)
        return {
            "slug": slug,
            "legs_up": pair.legs_up,
            "legs_down": pair.legs_down,
            "pair_cost": pair.pair_cost(),
            "potential_profit": pair.potential_profit(),
            "completed": pair.completed,
            "leg3_placed": pair.leg3_placed,
            "leg3_direction": pair.leg3_direction,
            "leg3_price": pair.leg3_price,
        }
