"""
Smart order manager with cancel/replace and fill tracking.

Key behaviors:
- Queues maker orders for placement
- Tracks all open orders and their status
- Cancels stale orders automatically
- Detects fills via polling (when WS fill events unavailable)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from core.polymarket_client import PolymarketClient, OrderResult

log = logging.getLogger(__name__)


@dataclass
class ManagedOrder:
    order_id: str
    token_id: str
    side: str           # BUY | SELL
    price: float
    size: float         # USDC
    strategy: str
    placed_at: float = field(default_factory=time.time)
    cancel_after: Optional[float] = None   # auto-cancel at this timestamp
    filled: bool = False
    cancelled: bool = False
    simulated: bool = False

    def is_expired(self) -> bool:
        if self.cancel_after is None:
            return False
        return time.time() > self.cancel_after

    def age(self) -> float:
        return time.time() - self.placed_at


class OrderManager:
    """
    Central order registry. Strategies call place_order() and cancel_order_by_id().
    Background task auto-cancels expired orders and polls for fills.
    """

    def __init__(self, client: PolymarketClient):
        self._client = client
        self._orders: dict[str, ManagedOrder] = {}
        self._fill_callbacks: list = []
        self._running = False
        self._lock = asyncio.Lock()

    def on_fill(self, callback):
        """Register callback: async def callback(order: ManagedOrder)"""
        self._fill_callbacks.append(callback)

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        strategy: str,
        cancel_after_secs: Optional[float] = None,
        fee_rate_bps: int = 0,
    ) -> Optional[ManagedOrder]:
        """Place a maker order and register it for tracking."""
        result: OrderResult = await self._client.place_maker_order(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            fee_rate_bps=fee_rate_bps,
        )

        if not result.success:
            log.error("Order placement failed (%s): %s", strategy, result.error)
            return None

        cancel_at = (time.time() + cancel_after_secs) if cancel_after_secs else None
        order = ManagedOrder(
            order_id=result.order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            strategy=strategy,
            cancel_after=cancel_at,
            simulated=result.simulated,
        )

        async with self._lock:
            self._orders[result.order_id] = order

        log.debug("Registered order %s from %s", result.order_id, strategy)
        return order

    async def cancel_order_by_id(self, order_id: str) -> bool:
        ok = await self._client.cancel_order(order_id)
        async with self._lock:
            if order_id in self._orders:
                self._orders[order_id].cancelled = True
        return ok

    async def cancel_by_strategy(self, strategy: str) -> int:
        """Cancel all open orders from a specific strategy."""
        to_cancel = [
            o for o in self._orders.values()
            if o.strategy == strategy and not o.filled and not o.cancelled
        ]
        for order in to_cancel:
            await self.cancel_order_by_id(order.order_id)
        return len(to_cancel)

    async def cancel_all(self) -> bool:
        ok = await self._client.cancel_all_orders()
        async with self._lock:
            for order in self._orders.values():
                if not order.filled:
                    order.cancelled = True
        return ok

    def get_open_orders(self, strategy: Optional[str] = None) -> list[ManagedOrder]:
        return [
            o for o in self._orders.values()
            if not o.filled and not o.cancelled
            and (strategy is None or o.strategy == strategy)
        ]

    def count_open(self, strategy: Optional[str] = None) -> int:
        return len(self.get_open_orders(strategy))

    # ── Background maintenance ────────────────────────────────────────────────

    async def run(self):
        """Background loop: expire stale orders, poll for fills."""
        self._running = True
        log.info("OrderManager background loop started")

        while self._running:
            try:
                await self._expire_orders()
                if config.BOT_MODE == "live":
                    await self._poll_fills()
                else:
                    await self._simulate_fills()
            except Exception as e:
                log.error("OrderManager error: %s", e)
            await asyncio.sleep(2)

    async def _expire_orders(self):
        to_cancel = []
        async with self._lock:
            for order in list(self._orders.values()):
                if not order.filled and not order.cancelled and order.is_expired():
                    to_cancel.append(order.order_id)

        for oid in to_cancel:
            log.info("Auto-cancelling expired order %s", oid)
            await self.cancel_order_by_id(oid)

    async def _simulate_fills(self):
        """Dry-run mode: mark simulated orders as filled after a realistic delay."""
        _FILL_DELAY = 2.0  # seconds — mimics typical maker fill latency
        async with self._lock:
            to_fill = [
                o for o in self._orders.values()
                if o.simulated and not o.filled and not o.cancelled
                and o.age() >= _FILL_DELAY
            ]
            for order in to_fill:
                order.filled = True

        for order in to_fill:
            log.info("Simulated fill: %s (strategy=%s size=$%.2f)",
                     order.order_id, order.strategy, order.size)
            for cb in self._fill_callbacks:
                asyncio.create_task(cb(order))

    async def _poll_fills(self):
        """Check which tracked orders have been filled via API."""
        open_orders = self.get_open_orders()
        if not open_orders:
            return

        try:
            live_orders = await self._client.get_open_orders()
            live_ids = {o.get("id") or o.get("orderID") for o in live_orders}

            async with self._lock:
                for order in open_orders:
                    if order.order_id not in live_ids and not order.simulated:
                        order.filled = True
                        log.info("Order %s filled (strategy=%s)", order.order_id, order.strategy)
                        for cb in self._fill_callbacks:
                            asyncio.create_task(cb(order))
        except Exception as e:
            log.warning("Fill poll error: %s", e)

    def stop(self):
        self._running = False
