"""
Tests for risk management: bankroll manager, risk limits, circuit breaker.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestBankrollManager(unittest.TestCase):
    def setUp(self):
        from risk.bankroll_manager import BankrollManager
        self.bm = BankrollManager(100.0)

    def test_initial_state(self):
        self.assertEqual(self.bm.bankroll, 100.0)
        self.assertEqual(self.bm.available, 100.0)
        self.assertEqual(self.bm._allocated, 0.0)

    def test_kelly_positive_edge(self):
        """Positive edge → positive bet size.
        At 80% win prob, entry at $0.70 → payout = 0.30/0.70 ≈ 0.429
        edge = 0.80 * 0.429 - 0.20 = 0.143 > 0  ✓
        """
        size = self.bm.kelly_size(win_prob=0.80, payout_ratio=0.429, fraction=0.5)
        self.assertGreater(size, 0.0)
        self.assertLessEqual(size, 10.0)  # capped at MAX_ABS_BET

    def test_kelly_negative_edge_returns_zero(self):
        """Negative edge → zero bet."""
        size = self.bm.kelly_size(win_prob=0.30, payout_ratio=0.111, fraction=0.5)
        self.assertEqual(size, 0.0)

    def test_flat_size_capped(self):
        size = self.bm.flat_size(0.50)  # ask for 50%, should be capped at 10%
        self.assertLessEqual(size, 10.0)

    def test_allocate_reduces_available(self):
        self.bm.allocate(20.0)
        self.assertEqual(self.bm.available, 80.0)

    def test_deallocate_restores_available(self):
        self.bm.allocate(20.0)
        self.bm.deallocate(20.0)
        self.assertAlmostEqual(self.bm.available, 100.0, places=4)

    def test_realize_pnl_positive(self):
        self.bm.realize_pnl(5.0)
        self.assertEqual(self.bm.bankroll, 105.0)

    def test_realize_pnl_negative(self):
        self.bm.realize_pnl(-10.0)
        self.assertEqual(self.bm.bankroll, 90.0)

    def test_is_solvent_above_minimum(self):
        self.assertTrue(self.bm.is_solvent())

    def test_is_solvent_below_minimum(self):
        self.bm.realize_pnl(-85.0)  # bankroll = 15, below MIN_BANKROLL=20
        self.assertFalse(self.bm.is_solvent())

    def test_allocation_caps_at_available(self):
        """Cannot allocate more than available."""
        size = self.bm.kelly_size(0.99, 10.0, 0.5)
        self.assertLessEqual(size, self.bm.available)


class TestDrawdownGuard(unittest.TestCase):
    def setUp(self):
        from risk.drawdown_guard import DrawdownGuard
        self.guard = DrawdownGuard()

    def test_no_circuit_initially(self):
        self.assertFalse(self.guard.is_circuit_open)
        ok, _ = self.guard.can_trade()
        self.assertTrue(ok)

    def test_circuit_trips_after_n_losses(self):
        import config
        for _ in range(config.CIRCUIT_BREAK_LOSSES):
            self.guard.record_loss()
        self.assertTrue(self.guard.is_circuit_open)

    def test_size_halved_after_2_losses(self):
        self.guard.record_loss()
        self.guard.record_loss()
        self.assertEqual(self.guard.size_multiplier, 0.5)

    def test_size_restores_after_wins(self):
        self.guard.record_loss()
        self.guard.record_loss()
        self.assertEqual(self.guard.size_multiplier, 0.5)
        for _ in range(5):
            self.guard.record_win()
        self.assertEqual(self.guard.size_multiplier, 1.0)

    def test_daily_loss_trips_circuit(self):
        import config
        threshold = -(config.STARTING_BANKROLL * config.CIRCUIT_BREAK_DAILY_LOSS)
        self.guard.check_daily_loss(threshold - 0.01)
        self.assertTrue(self.guard.is_circuit_open)

    def test_circuit_auto_resets_after_cooldown(self):
        """Circuit should reset after cooldown period (tested with mock time)."""
        import config
        self.guard.record_loss()
        self.guard.record_loss()
        self.guard.record_loss()
        self.assertTrue(self.guard.is_circuit_open)

        # Simulate time passing beyond cooldown
        self.guard._circuit_open_at = time.time() - (config.COOLDOWN_SECONDS + 1)
        self.assertFalse(self.guard.is_circuit_open)

    def test_cooldown_remaining_decrements(self):
        self.guard.record_loss()
        self.guard.record_loss()
        self.guard.record_loss()
        remaining = self.guard.cooldown_remaining
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, 900)


class TestRiskLimits(unittest.TestCase):
    def _make_limits(self, bankroll=100.0, daily_pnl=0.0, open_positions=0):
        from risk.bankroll_manager import BankrollManager
        from risk.risk_limits import RiskLimits

        bm = BankrollManager(bankroll)
        tracker = MagicMock()
        tracker.summary.return_value = {"daily_pnl": daily_pnl}
        tracker.open_positions.return_value = [None] * open_positions

        return RiskLimits(bm, tracker)

    def test_allows_valid_trade(self):
        limits = self._make_limits()
        ok, reason = limits.check(5.0, "test")
        self.assertTrue(ok)

    def test_rejects_below_minimum_size(self):
        limits = self._make_limits()
        ok, reason = limits.check(1.0, "test")  # $1 < $2.50 minimum
        self.assertFalse(ok)
        self.assertIn("too small", reason.lower())

    def test_rejects_daily_loss_exceeded(self):
        limits = self._make_limits(daily_pnl=-25.0)  # 25% loss on $100
        ok, reason = limits.check(5.0, "test")
        self.assertFalse(ok)
        self.assertIn("daily loss", reason.lower())

    def test_rejects_max_positions(self):
        import config
        limits = self._make_limits(open_positions=config.MAX_CONCURRENT_POSITIONS)
        ok, reason = limits.check(5.0, "test")
        self.assertFalse(ok)
        self.assertIn("concurrent", reason.lower())

    def test_max_allowed_size(self):
        limits = self._make_limits()
        max_size = limits.max_allowed_size()
        self.assertGreater(max_size, 0)
        self.assertLessEqual(max_size, 10.0)


class TestPositionTrackerExpiry(unittest.TestCase):
    """Stale positions must be force-settled so the position limit doesn't block trading."""

    def _make_position(self, slug, close_ts, direction="UP"):
        from core.position_tracker import Position
        return Position(
            window_slug=slug,
            token_id="0xtoken",
            direction=direction,
            strategy="pair_cost_avg",
            shares=5.0,
            entry_price=0.20,
            size_usdc=5.0,
            window_close_ts=close_ts,
        )

    def test_push_expiry_after_grace(self):
        """Position whose window closed > 120s ago should be force-settled as PUSH."""
        from core.position_tracker import PositionTracker
        tracker = PositionTracker()
        pos = self._make_position("btc-updown-5m-111", time.time() - 200)
        tracker.add_position(pos)

        tracker._expire_stale_positions()

        self.assertTrue(pos.closed)
        self.assertEqual(pos.outcome, "PUSH")
        self.assertEqual(pos.realized_pnl, 0.0)

    def test_no_expiry_within_grace(self):
        """Position whose window closed < 30s ago must NOT be force-settled yet."""
        from core.position_tracker import PositionTracker
        tracker = PositionTracker()
        pos = self._make_position("btc-updown-5m-222", time.time() - 10)
        tracker.add_position(pos)

        tracker._expire_stale_positions()

        self.assertFalse(pos.closed)

    def test_push_settle_zeroes_pnl(self):
        """PUSH outcome must have zero realized PnL."""
        from core.position_tracker import PositionTracker, WindowResult
        tracker = PositionTracker()
        pos = self._make_position("btc-updown-5m-333", time.time() - 200)
        tracker.add_position(pos)
        result = WindowResult(slug=pos.window_slug, direction="PUSH", resolved_at=time.time())
        tracker._settle(pos, result)
        self.assertEqual(pos.outcome, "PUSH")
        self.assertEqual(pos.realized_pnl, 0.0)
        self.assertTrue(pos.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
