"""
Bankroll manager — Half-Kelly position sizing with hard caps.

Kelly Criterion:
    edge = win_prob * payout - (1 - win_prob)
    kelly = edge / payout
    half_kelly = kelly * 0.5

Final size = min(bankroll * half_kelly, bankroll * MAX_BET_FRACTION, MAX_ABS_BET)
"""

import logging

import config

log = logging.getLogger(__name__)

MAX_ABS_BET = 10.0       # Never bet more than $10 even if Kelly says so


class BankrollManager:
    def __init__(self, starting_bankroll: float = config.STARTING_BANKROLL):
        self._bankroll = starting_bankroll
        self._starting = starting_bankroll
        self._allocated: float = 0.0     # USDC currently in open positions

    @property
    def bankroll(self) -> float:
        return self._bankroll

    @property
    def available(self) -> float:
        """Available capital (bankroll minus allocated in open trades)."""
        return max(0.0, self._bankroll - self._allocated)

    @property
    def allocation_fraction(self) -> float:
        return self._allocated / self._bankroll if self._bankroll > 0 else 0.0

    def is_solvent(self) -> bool:
        return self._bankroll >= config.MIN_BANKROLL

    def kelly_size(
        self,
        win_prob: float,
        payout_ratio: float = 1.0,
        fraction: float = 0.5,
    ) -> float:
        """
        Calculate position size via Half-Kelly.

        payout_ratio: net payout per unit risked.
                      e.g. buy at $0.90 → payout = (1.0 - 0.90) / 0.90 ≈ 0.111
                      or simpler: if entry price is p, payout = (1-p)/p
        """
        if win_prob <= 0 or payout_ratio <= 0:
            return 0.0

        edge = win_prob * payout_ratio - (1.0 - win_prob)
        if edge <= 0:
            return 0.0

        kelly_pct = edge / payout_ratio
        target_pct = kelly_pct * fraction

        raw = self._bankroll * target_pct
        capped = min(raw, self._bankroll * config.MAX_BET_FRACTION, MAX_ABS_BET)
        # Also cap by available capital
        capped = min(capped, self.available)
        return round(max(0.0, capped), 2)

    def flat_size(self, fraction: float) -> float:
        """Simple flat fractional bet (no Kelly)."""
        size = self._bankroll * min(fraction, config.MAX_BET_FRACTION)
        size = min(size, MAX_ABS_BET, self.available)
        return round(max(0.0, size), 2)

    def allocate(self, amount: float):
        """Mark capital as allocated to an open position."""
        self._allocated = round(self._allocated + amount, 4)

    def deallocate(self, amount: float):
        """Release capital from a closed/cancelled position."""
        self._allocated = round(max(0.0, self._allocated - amount), 4)

    def realize_pnl(self, pnl: float):
        """Update bankroll after position resolution."""
        self._bankroll = round(self._bankroll + pnl, 4)
        log.info("PnL realized: %+.4f USDC | Bankroll: %.4f USDC", pnl, self._bankroll)

    def summary(self) -> dict:
        return {
            "bankroll": self._bankroll,
            "starting": self._starting,
            "allocated": self._allocated,
            "available": self.available,
            "return_pct": (self._bankroll - self._starting) / self._starting * 100,
        }
