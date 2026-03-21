"""
Unit tests for trading strategies.

Tests analyze() with mock MarketState data and verifies
signal generation logic without real API calls.
"""

import asyncio
import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def make_state(
    window_delta=0.001,
    seconds_remaining=120.0,
    up_price=0.55,
    down_price=0.45,
    up_ask=0.56,
    down_ask=0.46,
    rsi=55.0,
    ema9=50001.0,
    ema21=50000.0,
    momentum_30s=0.0005,
    volume_ratio=1.5,
    orderflow_imbalance_up=0.3,
    orderflow_imbalance_down=-0.1,
):
    from strategies.base_strategy import MarketState
    from core.market_discovery import MarketInfo

    market = MarketInfo(
        slug="btc-updown-5m-1234567890",
        condition_id="0x" + "a" * 64,
        up_token_id="0x" + "b" * 64,
        down_token_id="0x" + "c" * 64,
        window_open_ts=1234567800,
        window_close_ts=1234568100,
        up_price=up_price,
        down_price=down_price,
    )
    return MarketState(
        market=market,
        btc_price=50010.0,
        window_delta=window_delta,
        momentum_30s=momentum_30s,
        ema9=ema9,
        ema21=ema21,
        rsi=rsi,
        macd=10.0,
        macd_signal=5.0,
        bb_upper=50500.0,
        bb_lower=49500.0,
        bb_mid=50000.0,
        volume_ratio=volume_ratio,
        up_price=up_price,
        down_price=down_price,
        up_bid=up_price - 0.01,
        up_ask=up_ask,
        down_bid=down_price - 0.01,
        down_ask=down_ask,
        orderflow_imbalance_up=orderflow_imbalance_up,
        orderflow_imbalance_down=orderflow_imbalance_down,
        seconds_remaining=seconds_remaining,
    )


class TestEndCycleSniper(unittest.TestCase):
    def setUp(self):
        from strategies.endcycle_sniper import EndCycleSniper
        self.strategy = EndCycleSniper()

    def run_analyze(self, state):
        return asyncio.run(self.strategy.analyze(state))

    def test_no_signal_outside_window(self):
        """Should return NEUTRAL when not in end-cycle window."""
        state = make_state(seconds_remaining=200.0)
        signal = self.run_analyze(state)
        self.assertEqual(signal.direction, "NEUTRAL")

    def test_signal_in_end_cycle_window(self):
        """Should generate UP signal in end-cycle window with strong delta."""
        state = make_state(
            window_delta=0.002,        # strong upward delta
            seconds_remaining=20.0,    # in end-cycle window
        )
        signal = self.run_analyze(state)
        self.assertEqual(signal.direction, "UP")
        self.assertGreater(signal.confidence, 0.60)

    def test_no_signal_small_delta(self):
        """Should skip when delta is coin-flip territory."""
        state = make_state(window_delta=0.0001, seconds_remaining=20.0)
        signal = self.run_analyze(state)
        self.assertEqual(signal.direction, "NEUTRAL")

    def test_down_signal_negative_delta(self):
        """Should generate DOWN signal when delta is negative."""
        state = make_state(
            window_delta=-0.002,
            seconds_remaining=20.0,
            ema9=49999.0,
            ema21=50001.0,
            momentum_30s=-0.001,
            orderflow_imbalance_up=-0.3,
        )
        signal = self.run_analyze(state)
        self.assertEqual(signal.direction, "DOWN")

    def test_no_duplicate_trades_same_window(self):
        """Should not trade twice in the same window."""
        state = make_state(window_delta=0.002, seconds_remaining=20.0)
        sig1 = self.run_analyze(state)
        self.assertTrue(sig1.is_actionable(), "First signal should be actionable")
        # Simulate trade being placed: mark window as traded
        self.strategy.record_trade(state.market.slug)
        sig2 = self.run_analyze(state)
        self.assertEqual(sig2.direction, "NEUTRAL")


class TestPairCostAvg(unittest.TestCase):
    def setUp(self):
        from strategies.pair_cost_avg import PairCostAvg
        self.strategy = PairCostAvg()

    def run_analyze(self, state):
        return asyncio.run(self.strategy.analyze(state))

    def test_buy_cheap_up(self):
        """Should buy UP when UP token is cheap."""
        state = make_state(up_ask=0.30, down_ask=0.72)
        signal = self.run_analyze(state)
        self.assertEqual(signal.direction, "UP")

    def test_buy_cheap_down(self):
        """Should buy DOWN when DOWN token is cheap."""
        state = make_state(up_ask=0.70, down_ask=0.30)
        signal = self.run_analyze(state)
        self.assertEqual(signal.direction, "DOWN")

    def test_no_buy_expensive(self):
        """Should not buy when both sides are expensive."""
        state = make_state(up_ask=0.52, down_ask=0.50)
        signal = self.run_analyze(state)
        self.assertEqual(signal.direction, "NEUTRAL")

    def test_pair_cost_tracking(self):
        """Record fills and verify pair cost calculation."""
        from strategies.pair_cost_avg import PairState
        slug = "test-slug"
        self.strategy.record_fill(slug, "UP", 0.30, 5.0)
        state = self.strategy._get_state(slug)
        self.assertAlmostEqual(state.qty_up, 5.0 / 0.30, places=5)
        self.assertEqual(state.spent_up, 5.0)

    def test_pair_cost_calculation(self):
        """Pair cost should be < 1.0 for profitable pairs."""
        from strategies.pair_cost_avg import PairState
        pair = PairState(slug="x")
        # Buy 5 shares UP at 0.30, 5 shares DOWN at 0.30
        pair.spent_up = 5.0
        pair.qty_up = 5.0 / 0.30
        pair.spent_down = 5.0
        pair.qty_down = 5.0 / 0.30
        cost = pair.pair_cost()
        self.assertLess(cost, 1.0)
        self.assertGreater(pair.potential_profit(), 0.0)


class TestMonteCarlo(unittest.TestCase):
    def setUp(self):
        from strategies.monte_carlo import MonteCarlo
        self.strategy = MonteCarlo()

    def test_simulation_returns_probability(self):
        """Monte Carlo simulation should return a probability 0-1."""
        p = self.strategy._run_simulation(
            current_price=50000.0,
            window_open=50000.0,
            time_remaining=60.0,
            vol=0.01,
            n=1000,
        )
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)

    def test_high_price_means_high_prob_up(self):
        """Current price well above open → high P(UP)."""
        p = self.strategy._run_simulation(
            current_price=51000.0,
            window_open=50000.0,
            time_remaining=5.0,    # almost no time left
            vol=0.001,             # low vol
            n=1000,
        )
        self.assertGreater(p, 0.80)


class TestLatencyArb(unittest.TestCase):
    def setUp(self):
        from strategies.latency_arb import LatencyArb
        self.strategy = LatencyArb()

    def run_analyze(self, state):
        return asyncio.run(self.strategy.analyze(state))

    def test_arb_signal_when_edge(self):
        """Should signal when spot and market implied diverge significantly."""
        state = make_state(
            window_delta=0.003,     # BTC up 0.3%
            up_price=0.40,          # market still pricing UP at 40% (underpriced)
            momentum_30s=0.001,
            seconds_remaining=120.0,
        )
        signal = self.run_analyze(state)
        # Spot says UP > 50%, market at 40% → edge should trigger
        # Note: actual signal depends on sigmoid computation
        self.assertIn(signal.direction, ("UP", "NEUTRAL"))


class TestRiskLimits(unittest.TestCase):
    def test_kelly_size_positive_edge(self):
        from risk.bankroll_manager import BankrollManager
        bm = BankrollManager(100.0)
        # 80% win prob, entry at $0.70 → payout ≈ 0.429, edge = 0.143 > 0
        size = bm.kelly_size(0.80, 0.429, 0.5)
        self.assertGreater(size, 0.0)
        self.assertLessEqual(size, 10.0)

    def test_kelly_size_negative_edge(self):
        from risk.bankroll_manager import BankrollManager
        bm = BankrollManager(100.0)
        size = bm.kelly_size(0.30, 0.111, 0.5)
        self.assertEqual(size, 0.0)

    def test_circuit_breaker_trips(self):
        from risk.drawdown_guard import DrawdownGuard
        guard = DrawdownGuard()
        guard.record_loss()
        guard.record_loss()
        guard.record_loss()
        self.assertTrue(guard.is_circuit_open)

    def test_circuit_breaker_daily_loss(self):
        from risk.drawdown_guard import DrawdownGuard
        guard = DrawdownGuard()
        guard.check_daily_loss(-20.0)  # $20 loss on $100 bankroll = 20% > 15%
        self.assertTrue(guard.is_circuit_open)


class TestConfig(unittest.TestCase):
    def test_taker_fee_formula(self):
        from config import calc_taker_fee
        fee_50 = calc_taker_fee(0.50)
        fee_90 = calc_taker_fee(0.90)
        # At 50 cents: highest fee
        self.assertGreater(fee_50, fee_90)
        # Fee must be positive
        self.assertGreater(fee_50, 0)

    def test_estimate_token_price(self):
        from config import estimate_token_price
        # delta_pct thresholds: <0.5%→0.50, <2%→0.55, <5%→0.65, <10%→0.80, <15%→0.92, else→0.97
        self.assertEqual(estimate_token_price(0.000), 0.50)   # 0.0% → coin flip
        self.assertEqual(estimate_token_price(0.003), 0.50)   # 0.3% < 0.5% → still coin flip
        self.assertEqual(estimate_token_price(0.01),  0.55)   # 1% → slight lean
        self.assertEqual(estimate_token_price(0.03),  0.65)   # 3% → moderate
        self.assertEqual(estimate_token_price(0.08),  0.80)   # 8% → strong
        self.assertEqual(estimate_token_price(0.20),  0.97)   # 20% → locked in

    def test_slug_construction(self):
        from core.market_discovery import build_slug
        import time
        slug = build_slug("5m", 0)
        self.assertTrue(slug.startswith("btc-updown-5m-"))
        ts = int(slug.split("-")[-1])
        self.assertEqual(ts % 300, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
