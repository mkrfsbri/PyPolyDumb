"""
Drawdown guard — circuit breaker to halt trading after losses.

Triggers:
  - N consecutive losses (default: 3)
  - Daily loss > X% of starting bankroll (default: 15%)

Behavior after trigger:
  - Pause all trading for COOLDOWN_SECONDS (default: 15 min)
  - After cooldown, resume with halved bet sizes for 5 trades
  - Auto-reduce multiplier applied to Kelly sizing
"""

import asyncio
import logging
import time
from typing import Optional

import config

log = logging.getLogger(__name__)


class DrawdownGuard:
    def __init__(self):
        self._consecutive_losses: int = 0
        self._circuit_open: bool = False
        self._circuit_open_at: Optional[float] = None
        self._size_multiplier: float = 1.0
        self._reduced_trades_remaining: int = 0

    @property
    def is_circuit_open(self) -> bool:
        if not self._circuit_open:
            return False
        # Auto-reset after cooldown
        if time.time() - (self._circuit_open_at or 0) >= config.COOLDOWN_SECONDS:
            self._reset_circuit()
            return False
        return True

    @property
    def size_multiplier(self) -> float:
        """Apply this to position sizes (0.5 after recent losses, 1.0 normally)."""
        return self._size_multiplier

    @property
    def cooldown_remaining(self) -> float:
        if not self._circuit_open:
            return 0.0
        elapsed = time.time() - (self._circuit_open_at or time.time())
        return max(0.0, config.COOLDOWN_SECONDS - elapsed)

    def record_win(self):
        self._consecutive_losses = 0
        if self._reduced_trades_remaining > 0:
            self._reduced_trades_remaining -= 1
            if self._reduced_trades_remaining == 0:
                self._size_multiplier = 1.0
                log.info("DrawdownGuard: size multiplier restored to 1.0")

    def record_loss(self):
        self._consecutive_losses += 1
        log.warning("DrawdownGuard: consecutive losses = %d", self._consecutive_losses)

        # Soft limit: halve size after 2 consecutive losses
        if self._consecutive_losses >= 2 and self._size_multiplier == 1.0:
            self._size_multiplier = 0.5
            self._reduced_trades_remaining = 5
            log.warning("DrawdownGuard: size halved for next 5 trades")

        # Hard limit: circuit breaker at N consecutive losses
        if self._consecutive_losses >= config.CIRCUIT_BREAK_LOSSES:
            self._trip_circuit(f"{self._consecutive_losses} consecutive losses")

    def check_daily_loss(self, daily_pnl: float):
        """Call this periodically with current daily P&L."""
        threshold = -(config.STARTING_BANKROLL * config.CIRCUIT_BREAK_DAILY_LOSS)
        if daily_pnl < threshold and not self._circuit_open:
            self._trip_circuit(f"daily loss ${-daily_pnl:.2f} exceeded threshold")

    def can_trade(self) -> tuple[bool, str]:
        """Returns (True, '') if trading is allowed, else (False, reason)."""
        if self.is_circuit_open:
            remaining = self.cooldown_remaining
            return False, f"Circuit breaker open — cooldown {remaining:.0f}s remaining"
        return True, "ok"

    # ── Internal ──────────────────────────────────────────────────────────────

    def _trip_circuit(self, reason: str):
        if self._circuit_open:
            return
        self._circuit_open = True
        self._circuit_open_at = time.time()
        log.error("CIRCUIT BREAKER TRIPPED: %s — pausing for %ds",
                  reason, config.COOLDOWN_SECONDS)

    def _reset_circuit(self):
        self._circuit_open = False
        self._circuit_open_at = None
        self._consecutive_losses = 0
        self._size_multiplier = 0.5      # resume at half size
        self._reduced_trades_remaining = 5
        log.info("Circuit breaker reset — resuming at 50%% size for 5 trades")

    def status(self) -> dict:
        return {
            "circuit_open": self.is_circuit_open,
            "cooldown_remaining": self.cooldown_remaining,
            "consecutive_losses": self._consecutive_losses,
            "size_multiplier": self._size_multiplier,
            "reduced_trades_remaining": self._reduced_trades_remaining,
        }
