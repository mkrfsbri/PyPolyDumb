"""
Polymarket CLOB API wrapper.

Wraps py-clob-client and adds:
- dry_run / paper mode guards
- Maker-only order placement with feeRateBps
- Exponential backoff on network errors
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config

log = logging.getLogger(__name__)

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds,
        AssetType,
        BalanceAllowanceParams,
        BookParams,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
    )
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    log.warning("py-clob-client not installed — API calls will be simulated")


@dataclass
class OrderResult:
    order_id: str
    success: bool
    error: str = ""
    simulated: bool = False


@dataclass
class OrderbookSnapshot:
    token_id: str
    bids: list = field(default_factory=list)   # [(price, size), ...]
    asks: list = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def best_bid(self) -> Optional[float]:
        return max((p for p, _ in self.bids), default=None)

    def best_ask(self) -> Optional[float]:
        return min((p for p, _ in self.asks), default=None)

    def mid_price(self) -> Optional[float]:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid and ask:
            return (bid + ask) / 2.0
        return None


class PolymarketClient:
    """Thread-safe async wrapper around py-clob-client."""

    def __init__(self):
        self._client: Optional[object] = None
        self._mode = config.BOT_MODE
        self._sim_order_counter = 0

    def initialize(self) -> bool:
        """Initialize the CLOB client with credentials. Returns True on success."""
        if not CLOB_AVAILABLE:
            log.warning("Running without py-clob-client (simulated mode)")
            return True

        if not config.validate_credentials():
            log.warning("Credentials not configured — running in simulated mode")
            return True

        if not config.is_live_mode() and self._mode not in ("live", "paper"):
            log.info("Mode=%s — skipping real client init", self._mode)
            return True

        try:
            creds = ApiCreds(
                api_key=config.POLY_API_KEY,
                api_secret=config.POLY_API_SECRET,
                api_passphrase=config.POLY_API_PASSPHRASE,
            )
            self._client = ClobClient(
                host=config.CLOB_HOST,
                chain_id=137,           # Polygon mainnet
                key=config.POLY_PRIVATE_KEY,
                creds=creds,
                signature_type=config.POLY_SIGNATURE_TYPE,
                funder=config.POLY_FUNDER_ADDRESS,
            )
            self._sync_allowances()
            log.info("ClobClient initialized (mode=%s)", self._mode)
            return True
        except Exception as e:
            log.error("Failed to initialize ClobClient: %s", e)
            return False

    def _sync_allowances(self) -> None:
        """Sync on-chain USDC + conditional token allowances with CLOB server."""
        sig = config.POLY_SIGNATURE_TYPE
        for asset in (AssetType.COLLATERAL, AssetType.CONDITIONAL):
            try:
                self._client.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=asset, signature_type=sig)
                )
            except Exception as e:
                log.warning("update_balance_allowance(%s) failed: %s", asset, e)

    # ── Order placement ────────────────────────────────────────────────────────

    async def place_maker_order(
        self,
        token_id: str,
        side: str,          # "BUY" or "SELL"
        price: float,       # 0.0 – 1.0
        size: float,        # USDC amount (will be converted to shares)
        fee_rate_bps: int = 0,
    ) -> OrderResult:
        """Place a GTC limit (maker) order. fee_rate_bps=0 for maker."""
        shares = round(size / price, 2)
        shares = max(shares, config.POLY_MIN_SHARES)

        if self._mode == "dry_run" or not CLOB_AVAILABLE or self._client is None:
            return self._simulate_order(token_id, side, price, shares)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side=side,
            fee_rate_bps=fee_rate_bps,
        )
        options = PartialCreateOrderOptions(neg_risk=False)

        for attempt, delay in enumerate([0, 2, 4, 8]):
            if delay:
                await asyncio.sleep(delay)
            try:
                order = self._client.create_order(order_args, options)
                resp = self._client.post_order(order, OrderType.GTC)
                order_id = resp.get("orderID", "") or resp.get("id", "")
                log.info("Placed maker order %s: %s %s @ %.4f (%.2f shares)",
                         order_id, side, token_id[:8], price, shares)
                return OrderResult(order_id=order_id, success=True)
            except Exception as e:
                log.warning("Order attempt %d failed: %s", attempt + 1, e)
                if attempt == 3:
                    return OrderResult(order_id="", success=False, error=str(e))

        return OrderResult(order_id="", success=False, error="max retries exceeded")

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID."""
        if self._mode == "dry_run" or self._client is None:
            log.debug("DRY_RUN: cancel order %s", order_id)
            return True
        try:
            self._client.cancel(order_id)
            return True
        except Exception as e:
            log.warning("Cancel failed for %s: %s", order_id, e)
            return False

    async def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        if self._mode == "dry_run" or self._client is None:
            log.debug("DRY_RUN: cancel all orders")
            return True
        try:
            self._client.cancel_all()
            return True
        except Exception as e:
            log.warning("Cancel all failed: %s", e)
            return False

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_orderbook(self, token_id: str) -> OrderbookSnapshot:
        """Fetch current orderbook snapshot via REST."""
        if self._client is None or not CLOB_AVAILABLE:
            return OrderbookSnapshot(token_id=token_id)
        try:
            book = self._client.get_order_book(token_id)
            bids = [(float(b.price), float(b.size)) for b in (book.bids or [])]
            asks = [(float(a.price), float(a.size)) for a in (book.asks or [])]
            return OrderbookSnapshot(token_id=token_id, bids=bids, asks=asks)
        except Exception as e:
            log.warning("get_orderbook failed: %s", e)
            return OrderbookSnapshot(token_id=token_id)

    async def get_open_orders(self) -> list:
        if self._client is None:
            return []
        try:
            return self._client.get_orders() or []
        except Exception:
            return []

    async def get_balance(self) -> float:
        """Return USDC balance from Polymarket."""
        if self._client is None:
            return 0.0
        try:
            balance = self._client.get_balance()
            return float(balance)
        except Exception:
            return 0.0

    # ── Internal ──────────────────────────────────────────────────────────────

    def _simulate_order(self, token_id: str, side: str, price: float, shares: float) -> OrderResult:
        self._sim_order_counter += 1
        order_id = f"SIM-{self._sim_order_counter:06d}"
        log.info("SIMULATED ORDER %s: %s %s @ %.4f (%.2f shares)",
                 order_id, side, token_id[:8], price, shares)
        return OrderResult(order_id=order_id, success=True, simulated=True)
