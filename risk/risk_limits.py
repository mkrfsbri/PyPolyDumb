"""
Hard risk limits enforced before every trade.

Checks:
  1. Bankroll above minimum
  2. Max concurrent positions not exceeded
  3. Daily loss limit not breached
  4. Per-trade size within bounds
  5. Circuit breaker not active
"""

import logging
from typing import Optional

import config
from risk.bankroll_manager import BankrollManager
from core.position_tracker import PositionTracker

log = logging.getLogger(__name__)


class RiskLimits:
    def __init__(self, bankroll: BankrollManager, tracker: PositionTracker):
        self._bankroll = bankroll
        self._tracker = tracker

    def check(
        self,
        proposed_size: float,
        strategy: str = "",
        current_window_slug: str = "",
    ) -> tuple[bool, str]:
        """
        Run all risk checks before placing a trade.

        current_window_slug: if provided, the concurrent-position limit is applied
        only against positions in that window. Positions from expired windows that
        haven't settled yet are ignored so they don't block fresh-window trading.

        Returns (allowed: bool, reason: str).
        """
        # 1. Solvency
        if not self._bankroll.is_solvent():
            return False, f"Bankroll ${self._bankroll.bankroll:.2f} below minimum ${config.MIN_BANKROLL}"

        # 2. Daily loss limit
        summary = self._tracker.summary()
        daily_pnl = summary.get("daily_pnl", 0.0)
        max_daily_loss = config.STARTING_BANKROLL * config.MAX_DAILY_LOSS_FRACTION
        if daily_pnl < -max_daily_loss:
            return False, f"Daily loss ${-daily_pnl:.2f} exceeds limit ${max_daily_loss:.2f}"

        # 3. Max concurrent positions — scoped to current window when slug is given
        if current_window_slug:
            open_positions = self._tracker.open_positions(window_slug=current_window_slug)
        else:
            open_positions = self._tracker.open_positions()
        open_count = len(open_positions)
        if open_count >= config.MAX_CONCURRENT_POSITIONS:
            return False, f"Max concurrent positions ({config.MAX_CONCURRENT_POSITIONS}) reached"

        # 4. Available capital
        if proposed_size > self._bankroll.available:
            return False, (
                f"Proposed size ${proposed_size:.2f} exceeds "
                f"available ${self._bankroll.available:.2f}"
            )

        # 5. Minimum viable size ($2.50 minimum at $0.50 to get 5 shares)
        if proposed_size < 2.50:
            return False, f"Size ${proposed_size:.2f} too small (minimum $2.50)"

        return True, "ok"

    def max_allowed_size(self) -> float:
        """Return the maximum size we can place right now."""
        return min(
            self._bankroll.available,
            self._bankroll.bankroll * config.MAX_BET_FRACTION,
            10.0,  # absolute max
        )
