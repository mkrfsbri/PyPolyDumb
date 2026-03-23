"""
Strategy #2: Pair Cost Averaging (Gabagool Strategy)

Masuk kedua sisi (UP+DOWN) secara sequential dengan limit order di bawah ask,
sehingga pair_cost < 0.97 — profit terkunci terlepas dari outcome.

Flow 3 Leg:
  Leg 1 : Limit order di sisi lebih murah @ ask - PAIR_LEG_DISCOUNT
           Hanya masuk di ~45 detik pertama window (PAIR_MIN_ENTRY_SECS)
           TTL = PAIR_LEG1_TTL (120s)
  Leg 2 : Setelah Leg 1 fill — limit order sisi lain @ (0.97 - leg1_fill_price)
           Harga dinamis: selalu menghasilkan pair_cost tepat 0.97
           TTL = PAIR_LEG2_TTL (90s)
  Leg 3 : T-15s, setelah pair complete — beli sisi yang mendekati 1.0
           (near-certain winner momentum bet)

Ekonomi:
  Leg 1 fill @ 0.482, Leg 2 target = 0.97 - 0.482 = 0.488
  pair_cost = 0.970 → guaranteed +$0.30 per 10 shares
  Leg 3 @ 0.87 → gain = 10×(1.00-0.87) = +$1.30 jika menang
"""

import logging
import time
from dataclasses import dataclass, field

import config
from strategies.base_strategy import BaseStrategy, MarketState, Signal

log = logging.getLogger(__name__)

MAX_PAIR_COST    = config.PAIR_COST_MAX        # 0.97
LEG_DISCOUNT     = config.PAIR_LEG_DISCOUNT    # 0.02
LEG1_TTL         = config.PAIR_LEG1_TTL        # 120s
LEG2_TTL         = config.PAIR_LEG2_TTL        # 90s
MIN_ENTRY_SECS   = config.PAIR_MIN_ENTRY_SECS  # 255s — only enter in first ~45s of 5m window
LEG3_THRESHOLD   = config.PAIR_LEG3_THRESHOLD  # 0.85
LEG3_MAX_PRICE   = config.PAIR_LEG3_MAX_PRICE  # 0.99
LEG3_ACTIVATION  = config.PAIR_LEG3_ACTIVATION # 15s


@dataclass
class PairState:
    """Per-window state."""
    slug: str

    # Placement tracking (prevents duplicate orders before fill confirmation)
    legs_placed_up: int = 0
    legs_placed_down: int = 0
    leg1_direction: str = ""
    leg1_order_price: float = 0.0
    leg1_placed_at: float = 0.0     # timestamp when Leg 1 was placed

    # Fill tracking
    legs_filled_up: int = 0
    legs_filled_down: int = 0
    leg1_fill_price: float = 0.0    # actual fill price — used for dynamic Leg 2 pricing
    spent_up: float = 0.0
    spent_down: float = 0.0
    qty_up: float = 0.0
    qty_down: float = 0.0
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

    def leg1_placed(self) -> bool:
        return (self.legs_placed_up + self.legs_placed_down) == 1

    def leg1_filled(self) -> bool:
        return (self.legs_filled_up + self.legs_filled_down) >= 1

    def leg1_expired(self) -> bool:
        """Leg 1 was placed but TTL elapsed without fill."""
        if not self.leg1_placed() or self.leg1_filled():
            return False
        return time.time() - self.leg1_placed_at > LEG1_TTL + 5  # 5s grace

    def leg2_placed(self) -> bool:
        return self.legs_placed_up >= 1 and self.legs_placed_down >= 1


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
        if pair.completed and not pair.leg3_placed and secs <= LEG3_ACTIVATION:
            if LEG3_THRESHOLD <= up_ask < LEG3_MAX_PRICE:
                log.info("PairCostAvg Leg 3: UP @ %.3f (T-%.0fs)", up_ask, secs)
                return Signal(
                    direction="UP",
                    confidence=min(0.99, up_ask),
                    suggested_price=up_ask,
                    suggested_size=0.0,
                    reason=f"Leg 3 UP @ {up_ask:.3f} — T-{secs:.0f}s",
                    cancel_after_secs=secs,
                )
            if LEG3_THRESHOLD <= down_ask < LEG3_MAX_PRICE:
                log.info("PairCostAvg Leg 3: DOWN @ %.3f (T-%.0fs)", down_ask, secs)
                return Signal(
                    direction="DOWN",
                    confidence=min(0.99, down_ask),
                    suggested_price=down_ask,
                    suggested_size=0.0,
                    reason=f"Leg 3 DOWN @ {down_ask:.3f} — T-{secs:.0f}s",
                    cancel_after_secs=secs,
                )
            if up_ask >= LEG3_MAX_PRICE or down_ask >= LEG3_MAX_PRICE:
                log.info("PairCostAvg Leg 3: SKIP — %.3f/%.3f >= %.2f, no upside",
                         up_ask, down_ask, LEG3_MAX_PRICE)

        if pair.completed:
            return Signal(reason="Pair complete — menunggu Leg 3 (T-15s)")

        # ── Leg 1 expired without fill → skip window ─────────────────────────
        if pair.leg1_expired():
            return Signal(reason="Leg 1 expired without fill — skip window")

        # ── Leg 1 placed, waiting for fill ───────────────────────────────────
        if pair.leg1_placed() and not pair.leg1_filled():
            return Signal(reason=f"Leg 1 {pair.leg1_direction} @ {pair.leg1_order_price:.3f} — waiting fill")

        # ── Leg 2: Leg 1 filled, place other side ────────────────────────────
        if pair.leg1_filled() and not pair.leg2_placed():
            min_secs_needed = LEG2_TTL + LEG3_ACTIVATION + 30  # 135s
            if secs < min_secs_needed:
                return Signal(
                    reason=f"Tidak cukup waktu Leg 2 ({secs:.0f}s < {min_secs_needed}s)"
                )

            # Dynamic pricing: exactly locks pair_cost = 0.97
            leg2_target = round(MAX_PAIR_COST - pair.leg1_fill_price, 3)
            if leg2_target <= 0.01:
                return Signal(reason=f"Leg 2 target tidak valid: {leg2_target:.3f}")

            if pair.legs_filled_up >= 1:
                log.info("PairCostAvg Leg 2: DOWN @ %.3f (0.97 - %.3f) ask=%.3f",
                         leg2_target, pair.leg1_fill_price, down_ask)
                return Signal(
                    direction="DOWN",
                    confidence=0.97,
                    suggested_price=leg2_target,
                    suggested_size=0.0,
                    reason=f"Leg 2 DOWN @ {leg2_target:.3f} = 0.97 - {pair.leg1_fill_price:.3f}",
                    cancel_after_secs=float(LEG2_TTL),
                )
            else:
                log.info("PairCostAvg Leg 2: UP @ %.3f (0.97 - %.3f) ask=%.3f",
                         leg2_target, pair.leg1_fill_price, up_ask)
                return Signal(
                    direction="UP",
                    confidence=0.97,
                    suggested_price=leg2_target,
                    suggested_size=0.0,
                    reason=f"Leg 2 UP @ {leg2_target:.3f} = 0.97 - {pair.leg1_fill_price:.3f}",
                    cancel_after_secs=float(LEG2_TTL),
                )

        # ── Leg 1: No position yet ────────────────────────────────────────────
        if pair.legs_placed_up == 0 and pair.legs_placed_down == 0:
            # Only enter in first part of window — need time for Leg 2 + Leg 3
            if secs < MIN_ENTRY_SECS:
                return Signal(
                    reason=f"Terlalu telat untuk Leg 1 ({secs:.0f}s < {MIN_ENTRY_SECS}s)"
                )

            # Sanity check: combined ask shouldn't be wildly wide
            combined_ask = up_ask + down_ask
            if combined_ask > 1.10:
                return Signal(reason=f"Spread terlalu lebar: combined ask={combined_ask:.3f}")

            # Buy cheaper side first — closer to target price, higher fill probability
            if up_ask <= down_ask:
                leg1_price = round(up_ask - LEG_DISCOUNT, 3)
                log.info("PairCostAvg Leg 1: UP @ %.3f (ask=%.3f - %.3f)",
                         leg1_price, up_ask, LEG_DISCOUNT)
                return Signal(
                    direction="UP",
                    confidence=0.97,
                    suggested_price=leg1_price,
                    suggested_size=0.0,
                    reason=f"Leg 1 UP @ {leg1_price:.3f} (ask={up_ask:.3f} - {LEG_DISCOUNT})",
                    cancel_after_secs=float(LEG1_TTL),
                )
            else:
                leg1_price = round(down_ask - LEG_DISCOUNT, 3)
                log.info("PairCostAvg Leg 1: DOWN @ %.3f (ask=%.3f - %.3f)",
                         leg1_price, down_ask, LEG_DISCOUNT)
                return Signal(
                    direction="DOWN",
                    confidence=0.97,
                    suggested_price=leg1_price,
                    suggested_size=0.0,
                    reason=f"Leg 1 DOWN @ {leg1_price:.3f} (ask={down_ask:.3f} - {LEG_DISCOUNT})",
                    cancel_after_secs=float(LEG1_TTL),
                )

        return Signal(reason="Kedua legs dipasang, menunggu fills")

    def should_trade(self, signal: Signal, state: MarketState) -> bool:
        return signal.is_actionable()

    def record_order_placed(self, slug: str, direction: str, price: float):
        """Dipanggil saat order dikirim — mencegah duplikat sebelum fill."""
        pair = self._get_state(slug)

        if pair.completed:
            pair.leg3_placed = True
            pair.leg3_direction = direction
            pair.leg3_price = price
            log.info("PairCostAvg Leg 3 placed: %s @ %.3f", direction, price)
            return

        if direction == "UP":
            pair.legs_placed_up += 1
        else:
            pair.legs_placed_down += 1

        # Track Leg 1 metadata
        if pair.legs_placed_up + pair.legs_placed_down == 1:
            pair.leg1_direction = direction
            pair.leg1_order_price = price
            pair.leg1_placed_at = time.time()

        log.info("PairCostAvg order placed: %s @ %.3f | placed_up=%d placed_down=%d",
                 direction, price, pair.legs_placed_up, pair.legs_placed_down)

    def record_fill(self, slug: str, direction: str, price: float, size_usdc: float):
        """Dipanggil saat order fill — update qty/spend dan simpan harga fill Leg 1."""
        pair = self._get_state(slug)
        shares = size_usdc / price

        # Leg 3 fill
        if pair.leg3_placed and pair.leg3_direction == direction and pair.leg3_qty == 0:
            pair.leg3_qty = shares
            pair.leg3_spent = size_usdc
            log.info("PairCostAvg Leg 3 fill: %s @ %.3f — %.2f shares ($%.2f)",
                     direction, price, shares, size_usdc)
            return

        # Leg 1 / 2 fill
        if direction == "UP":
            pair.spent_up += size_usdc
            pair.qty_up += shares
            pair.legs_filled_up += 1
        else:
            pair.spent_down += size_usdc
            pair.qty_down += shares
            pair.legs_filled_down += 1

        # Save actual fill price for dynamic Leg 2 pricing
        total_filled = pair.legs_filled_up + pair.legs_filled_down
        if total_filled == 1:
            pair.leg1_fill_price = price

        cost = pair.pair_cost()
        profit = pair.potential_profit()
        cost_str = "satu sisi saja" if cost >= 99.0 else f"{cost:.4f}"
        log.info("PairCostAvg fill: %s @ %.3f | pair_cost=%s potential_profit=%.4f USDC",
                 direction, price, cost_str, profit)

        if pair.qty_up > 0 and pair.qty_down > 0 and cost < MAX_PAIR_COST:
            pair.completed = True
            log.info("PairCostAvg: PAIR COMPLETE! slug=%s cost=%.4f profit_terkunci=%.4f USDC",
                     slug, cost, profit)

    def pair_summary(self, slug: str) -> dict:
        pair = self._get_state(slug)
        return {
            "slug": slug,
            "leg1_direction": pair.leg1_direction,
            "leg1_fill_price": pair.leg1_fill_price,
            "legs_placed": (pair.legs_placed_up, pair.legs_placed_down),
            "legs_filled": (pair.legs_filled_up, pair.legs_filled_down),
            "pair_cost": pair.pair_cost(),
            "potential_profit": pair.potential_profit(),
            "completed": pair.completed,
            "leg3_placed": pair.leg3_placed,
            "leg3_direction": pair.leg3_direction,
            "leg3_price": pair.leg3_price,
        }
