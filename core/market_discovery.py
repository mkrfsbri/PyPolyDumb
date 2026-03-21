"""
Gamma API market discovery for BTC Up/Down 5m / 15m markets.

Deterministic slug construction:
    window_ts = now - (now % 300)
    slug = f"btc-updown-5m-{window_ts}"

288 markets/day for 5m, 96 for 15m.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

import config

log = logging.getLogger(__name__)

GAMMA_MARKETS_URL = f"{config.GAMMA_API}/markets"


@dataclass
class MarketInfo:
    slug: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    window_open_ts: int     # unix timestamp of window start
    window_close_ts: int    # unix timestamp of window end
    up_price: float = 0.50
    down_price: float = 0.50

    def seconds_remaining(self) -> float:
        return max(0.0, self.window_close_ts - time.time())

    def is_active(self) -> bool:
        now = time.time()
        return self.window_open_ts <= now < self.window_close_ts


def build_slug(window_type: str = "5m", offset_windows: int = 0) -> str:
    """
    Build the deterministic slug for a BTC up/down market window.

    offset_windows=0 → current window
    offset_windows=1 → next window
    offset_windows=-1 → previous window
    """
    interval = config.WINDOW_SECONDS.get(window_type, 300)
    now = int(time.time())
    window_ts = now - (now % interval) + offset_windows * interval
    return f"btc-updown-{window_type}-{window_ts}"


async def fetch_market(slug: str, session: aiohttp.ClientSession) -> Optional[MarketInfo]:
    """Fetch market info from Gamma API by slug."""
    params = {"slug": slug}
    try:
        async with session.get(GAMMA_MARKETS_URL, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                log.warning("Gamma API returned %d for slug=%s", resp.status, slug)
                return None
            data = await resp.json()

            if not data:
                log.debug("No market found for slug=%s", slug)
                return None

            market = data[0] if isinstance(data, list) else data
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                log.warning("Market %s has <2 tokens", slug)
                return None

            # Identify UP and DOWN token IDs
            up_token_id = ""
            down_token_id = ""
            up_price = 0.50
            down_price = 0.50

            for token in tokens:
                outcome = (token.get("outcome") or "").upper()
                tid = token.get("token_id", "")
                price = float(token.get("price", 0.50))
                if "UP" in outcome or outcome == "YES":
                    up_token_id = tid
                    up_price = price
                elif "DOWN" in outcome or outcome == "NO":
                    down_token_id = tid
                    down_price = price

            if not up_token_id or not down_token_id:
                # Fallback: first token = UP, second = DOWN
                up_token_id = tokens[0].get("token_id", "")
                down_token_id = tokens[1].get("token_id", "")
                up_price = float(tokens[0].get("price", 0.50))
                down_price = float(tokens[1].get("price", 0.50))

            end_date = market.get("endDateIso") or market.get("end_date_iso") or ""
            try:
                import datetime
                dt = datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                close_ts = int(dt.timestamp())
            except Exception:
                interval = config.WINDOW_INTERVAL
                now_ts = int(time.time())
                close_ts = now_ts - (now_ts % interval) + interval

            open_ts = close_ts - config.WINDOW_INTERVAL

            return MarketInfo(
                slug=slug,
                condition_id=market.get("conditionId", market.get("condition_id", "")),
                up_token_id=up_token_id,
                down_token_id=down_token_id,
                window_open_ts=open_ts,
                window_close_ts=close_ts,
                up_price=up_price,
                down_price=down_price,
            )

    except asyncio.TimeoutError:
        log.warning("Timeout fetching market slug=%s", slug)
        return None
    except Exception as e:
        log.error("Error fetching market slug=%s: %s", slug, e)
        return None


async def get_current_market(window_type: str = "5m") -> Optional[MarketInfo]:
    """Return the currently active BTC up/down market."""
    async with aiohttp.ClientSession() as session:
        # Try current window; if not found, try next
        for offset in (0, 1, -1):
            slug = build_slug(window_type, offset)
            market = await fetch_market(slug, session)
            if market:
                log.info("Found market: %s (%.0fs remaining)", slug, market.seconds_remaining())
                return market
        log.error("Could not find any active BTC %s market", window_type)
        return None


async def get_next_market(window_type: str = "5m") -> Optional[MarketInfo]:
    """Pre-fetch the next window market so we can start immediately when it opens."""
    async with aiohttp.ClientSession() as session:
        slug = build_slug(window_type, 1)
        return await fetch_market(slug, session)


class MarketWatcher:
    """
    Continuously tracks the current active market window.
    Notifies when a new window opens.
    """

    def __init__(self, window_type: str = "5m"):
        self._window_type = window_type
        self._current: Optional[MarketInfo] = None
        self._on_new_window_callbacks: list = []
        self._running = False

    @property
    def current(self) -> Optional[MarketInfo]:
        return self._current

    def on_new_window(self, callback):
        """Register async callback: async def callback(market: MarketInfo)"""
        self._on_new_window_callbacks.append(callback)

    async def run(self):
        """Main loop — polls for new windows and fires callbacks."""
        self._running = True
        log.info("MarketWatcher started for %s markets", self._window_type)

        while self._running:
            try:
                market = await get_current_market(self._window_type)
                if market:
                    is_new = (
                        self._current is None
                        or self._current.slug != market.slug
                    )
                    self._current = market
                    if is_new:
                        log.info("New window opened: %s", market.slug)
                        for cb in self._on_new_window_callbacks:
                            try:
                                await cb(market)
                            except Exception as e:
                                log.error("Window callback error: %s", e)

                remaining = self._current.seconds_remaining() if self._current else 30
                # Sleep until near the end of the window, then poll more frequently
                if remaining > 60:
                    await asyncio.sleep(30)
                elif remaining > 15:
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(1)
            except Exception as e:
                log.error("MarketWatcher error: %s", e)
                await asyncio.sleep(5)

    def stop(self):
        self._running = False
