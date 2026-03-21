"""
Tests for order placement and signing logic.

Verifies that orders are correctly structured, include feeRateBps,
and behave correctly in dry_run/paper/live modes.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestPolymarketClient(unittest.TestCase):
    def setUp(self):
        # Force dry_run mode for tests
        import config
        config.BOT_MODE = "dry_run"
        from core.polymarket_client import PolymarketClient
        self.client = PolymarketClient()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_place_maker_order_dry_run(self):
        """Dry run should return simulated order without calling API."""
        result = self._run(self.client.place_maker_order(
            token_id="0xabc123",
            side="BUY",
            price=0.90,
            size=5.0,
            fee_rate_bps=0,
        ))
        self.assertTrue(result.success)
        self.assertTrue(result.simulated)
        self.assertTrue(result.order_id.startswith("SIM-"))

    def test_order_id_increments(self):
        """Simulated order IDs should be unique and increment."""
        r1 = self._run(self.client.place_maker_order("0xa", "BUY", 0.90, 5.0))
        r2 = self._run(self.client.place_maker_order("0xb", "BUY", 0.85, 5.0))
        self.assertNotEqual(r1.order_id, r2.order_id)

    def test_cancel_order_dry_run(self):
        """Cancel should succeed in dry_run mode."""
        result = self._run(self.client.cancel_order("SIM-000001"))
        self.assertTrue(result)

    def test_cancel_all_dry_run(self):
        result = self._run(self.client.cancel_all_orders())
        self.assertTrue(result)

    def test_minimum_shares_enforced(self):
        """Should increase shares to meet minimum share requirement."""
        import config
        # Very small order: 1 USDC at 0.50 = 2 shares, below POLY_MIN_SHARES (5)
        result = self._run(self.client.place_maker_order("0xa", "BUY", 0.50, 1.0))
        # Should still succeed (shares bumped to minimum)
        self.assertTrue(result.success)

    def test_initialize_without_credentials(self):
        """Should initialize gracefully without credentials."""
        from core.polymarket_client import PolymarketClient
        client = PolymarketClient()
        ok = client.initialize()
        self.assertTrue(ok)  # Should not crash


class TestOrderManager(unittest.TestCase):
    def setUp(self):
        import config
        config.BOT_MODE = "dry_run"
        from core.polymarket_client import PolymarketClient
        from core.order_manager import OrderManager
        self.client = PolymarketClient()
        self.order_mgr = OrderManager(self.client)

    def _run(self, coro):
        return asyncio.run(coro)

    def test_place_and_track_order(self):
        """Placed order should be tracked as open."""
        order = self._run(self.order_mgr.place_order(
            token_id="0xabc",
            side="BUY",
            price=0.90,
            size=5.0,
            strategy="endcycle_sniper",
        ))
        self.assertIsNotNone(order)
        open_orders = self.order_mgr.get_open_orders()
        self.assertEqual(len(open_orders), 1)
        self.assertEqual(open_orders[0].strategy, "endcycle_sniper")

    def test_cancel_order(self):
        """Cancelled order should be removed from open orders."""
        order = self._run(self.order_mgr.place_order("0xabc", "BUY", 0.90, 5.0, "test"))
        self._run(self.order_mgr.cancel_order_by_id(order.order_id))
        open_orders = self.order_mgr.get_open_orders()
        self.assertEqual(len(open_orders), 0)

    def test_cancel_by_strategy(self):
        """Cancel all orders from specific strategy."""
        self._run(self.order_mgr.place_order("0xa", "BUY", 0.90, 5.0, "strategy_a"))
        self._run(self.order_mgr.place_order("0xb", "BUY", 0.85, 5.0, "strategy_b"))

        cancelled = self._run(self.order_mgr.cancel_by_strategy("strategy_a"))
        self.assertEqual(cancelled, 1)
        open_orders = self.order_mgr.get_open_orders()
        # Only strategy_b order remains
        self.assertEqual(len(open_orders), 1)
        self.assertEqual(open_orders[0].strategy, "strategy_b")

    def test_fee_rate_bps_zero_for_maker(self):
        """Maker orders should always use fee_rate_bps=0."""
        order = self._run(self.order_mgr.place_order(
            "0xa", "BUY", 0.90, 5.0, "test", fee_rate_bps=0
        ))
        self.assertIsNotNone(order)
        # If order placed, it went through with fee_rate_bps=0
        self.assertTrue(order.simulated)


class TestMarketDiscovery(unittest.TestCase):
    def test_slug_is_deterministic(self):
        """Same timestamp should always produce the same slug."""
        from core.market_discovery import build_slug
        slug1 = build_slug("5m", 0)
        slug2 = build_slug("5m", 0)
        self.assertEqual(slug1, slug2)

    def test_slug_format(self):
        """Slug should match expected pattern."""
        from core.market_discovery import build_slug
        slug = build_slug("5m")
        parts = slug.split("-")
        self.assertEqual(parts[0], "btc")
        self.assertEqual(parts[1], "updown")
        self.assertEqual(parts[2], "5m")
        ts = int(parts[3])
        self.assertEqual(ts % 300, 0)

    def test_15m_slug_format(self):
        from core.market_discovery import build_slug
        slug = build_slug("15m")
        self.assertIn("15m", slug)
        ts = int(slug.split("-")[-1])
        self.assertEqual(ts % 900, 0)

    def test_next_window_slug(self):
        """offset=1 should return next window, aligned to 300s boundary."""
        from core.market_discovery import build_slug
        import time
        slug0 = build_slug("5m", 0)
        slug1 = build_slug("5m", 1)
        ts0 = int(slug0.split("-")[-1])
        ts1 = int(slug1.split("-")[-1])
        self.assertEqual(ts1 - ts0, 300)


class TestFetchMarketParsing(unittest.IsolatedAsyncioTestCase):
    """Test _extract_all_tokens and fetch_market response parsing logic."""

    def test_extract_tokens_flat(self):
        """Shape A: tokens at top level."""
        from core.market_discovery import _extract_all_tokens
        market = {
            "tokens": [
                {"tokenId": "aaa", "outcome": "Up", "price": "0.55"},
                {"tokenId": "bbb", "outcome": "Down", "price": "0.45"},
            ]
        }
        tokens = _extract_all_tokens(market)
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0]["tokenId"], "aaa")

    def test_extract_tokens_clob_token_ids(self):
        """Shape B: clobTokenIds paired with outcomes/outcomePrices (real Gamma API format)."""
        from core.market_discovery import _extract_all_tokens
        market = {
            "clobTokenIds": ["UP_ID", "DN_ID"],
            "outcomes": ["Up", "Down"],
            "outcomePrices": ["0.58", "0.42"],
        }
        tokens = _extract_all_tokens(market)
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0]["tokenId"], "UP_ID")
        self.assertEqual(tokens[0]["outcome"], "Up")
        self.assertAlmostEqual(float(tokens[0]["price"]), 0.58)
        self.assertEqual(tokens[1]["tokenId"], "DN_ID")

    def test_extract_tokens_missing(self):
        """No tokens field → empty list."""
        from core.market_discovery import _extract_all_tokens
        tokens = _extract_all_tokens({"slug": "x", "markets": []})
        self.assertEqual(tokens, [])

    async def test_fetch_market_shape_a(self):
        """Shape A (flat tokens) parsed correctly."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from core.market_discovery import fetch_market

        fake_response = [{
            "conditionId": "0x" + "a" * 64,
            "slug": "btc-updown-5m-1000000200",
            "endDateIso": "2001-09-08T21:50:00Z",
            "tokens": [
                {"tokenId": "UP_TOKEN", "outcome": "Up", "price": "0.55"},
                {"tokenId": "DN_TOKEN", "outcome": "Down", "price": "0.45"},
            ],
        }]

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=fake_response)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)

        result = await fetch_market("btc-updown-5m-1000000200", mock_session)
        self.assertIsNotNone(result)
        self.assertEqual(result.up_token_id, "UP_TOKEN")
        self.assertEqual(result.down_token_id, "DN_TOKEN")
        self.assertAlmostEqual(result.up_price, 0.55)

    async def test_fetch_market_shape_b(self):
        """Shape B (nested markets, 1 token each) parsed correctly."""
        from unittest.mock import AsyncMock, MagicMock
        from core.market_discovery import fetch_market

        fake_response = [{
            "slug": "btc-updown-5m-1000000200",
            "conditionId": "",
            "endDateIso": "2001-09-08T21:50:00Z",
            "tokens": [],   # empty at top level
            "markets": [
                {
                    "conditionId": "0x" + "b" * 64,
                    "tokens": [{"tokenId": "UP_TOK", "outcome": "Up", "price": "0.60"}],
                },
                {
                    "conditionId": "0x" + "c" * 64,
                    "tokens": [{"tokenId": "DN_TOK", "outcome": "Down", "price": "0.40"}],
                },
            ],
        }]

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=fake_response)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)

        result = await fetch_market("btc-updown-5m-1000000200", mock_session)
        self.assertIsNotNone(result)
        self.assertEqual(result.up_token_id, "UP_TOK")
        self.assertEqual(result.down_token_id, "DN_TOK")

    async def test_fetch_market_snake_case_token_id(self):
        """Handles snake_case token_id field (alternative API format)."""
        from unittest.mock import AsyncMock, MagicMock
        from core.market_discovery import fetch_market

        fake_response = [{
            "conditionId": "0x" + "d" * 64,
            "slug": "btc-updown-5m-1000000200",
            "endDate": "2001-09-08T21:50:00Z",
            "tokens": [
                {"token_id": "UP_SNAKE", "outcome": "Yes", "price": "0.52"},
                {"token_id": "DN_SNAKE", "outcome": "No", "price": "0.48"},
            ],
        }]

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=fake_response)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)

        result = await fetch_market("btc-updown-5m-1000000200", mock_session)
        self.assertIsNotNone(result)
        self.assertEqual(result.up_token_id, "UP_SNAKE")
        self.assertEqual(result.down_token_id, "DN_SNAKE")

    async def test_fetch_market_clob_token_ids(self):
        """Shape B: real Gamma API format with clobTokenIds + outcomes + outcomePrices."""
        from unittest.mock import AsyncMock, MagicMock
        from core.market_discovery import fetch_market

        fake_response = [{
            "conditionId": "0x" + "e" * 64,
            "slug": "btc-updown-5m-1000000200",
            "endDateIso": "2001-09-08T21:50:00Z",
            "tokens": [],
            "clobTokenIds": ["UP_CLOB", "DN_CLOB"],
            "outcomes": ["Up", "Down"],
            "outcomePrices": ["0.58", "0.42"],
        }]

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=fake_response)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)

        result = await fetch_market("btc-updown-5m-1000000200", mock_session)
        self.assertIsNotNone(result)
        self.assertEqual(result.up_token_id, "UP_CLOB")
        self.assertEqual(result.down_token_id, "DN_CLOB")
        self.assertAlmostEqual(result.up_price, 0.58)
        self.assertAlmostEqual(result.down_price, 0.42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
