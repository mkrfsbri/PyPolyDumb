"""
Chainlink oracle price monitoring (read-only, best-effort).

Polls the Chainlink BTC/USD aggregator on Polygon via a public RPC.
Used as a secondary price reference for latency arb strategy.

Note: This requires web3 or a JSON-RPC call. We use a simple HTTP
fallback to a public price API when web3 is unavailable.
"""

import asyncio
import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

# CoinGecko as Chainlink fallback
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

# Chainlink BTC/USD on Polygon (via public RPC)
POLYGON_RPC = "https://polygon-rpc.com"
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"


class ChainlinkFeed:
    """
    Polls external BTC price reference.
    Primary: CoinGecko fallback (no API key required)
    Secondary: Could integrate web3 for actual Chainlink aggregator
    """

    def __init__(self, poll_interval: float = 15.0):
        self._poll_interval = poll_interval
        self._price: float = 0.0
        self._last_update: float = 0.0
        self._running = False

    @property
    def price(self) -> float:
        return self._price

    @property
    def age(self) -> float:
        return time.time() - self._last_update

    def is_fresh(self, max_age: float = 30.0) -> bool:
        return self.age < max_age

    async def run(self):
        self._running = True
        log.info("ChainlinkFeed started (using CoinGecko fallback)")
        backoff = 15

        while self._running:
            try:
                price = await self._fetch_price()
                if price and price > 0:
                    self._price = price
                    self._last_update = time.time()
                    log.debug("ChainlinkFeed BTC: $%.2f", price)
                    backoff = self._poll_interval
                else:
                    backoff = min(backoff * 2, 120)
            except Exception as e:
                log.warning("ChainlinkFeed error: %s", e)
                backoff = min(backoff * 2, 120)

            await asyncio.sleep(backoff)

    async def _fetch_price(self) -> Optional[float]:
        params = {"ids": "bitcoin", "vs_currencies": "usd"}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                COINGECKO_URL, params=params, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["bitcoin"]["usd"])
        return None

    def stop(self):
        self._running = False
