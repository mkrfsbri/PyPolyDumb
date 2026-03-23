"""
Gamma API market discovery for BTC Up/Down 5m / 15m markets.

Deterministic slug construction:
    window_ts = now - (now % 300)
    slug = f"btc-updown-5m-{window_ts}"

288 markets/day for 5m, 96 for 15m.
"""

import asyncio
import json
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
    """Fetch market info from Gamma API by slug.

    Handles two Gamma API response shapes:

    Shape A — flat (tokens at market level):
        [{"conditionId": "...", "tokens": [{"tokenId"|"token_id": ..., "outcome": ...}, ...]}]

    Shape B — nested/event (each sub-market has 1 token):
        [{"slug": "...", "markets": [{"tokens": [{"tokenId": ..., "outcome": "Yes"}]}, ...]}]
    """
    params = {"slug": slug}
    try:
        async with session.get(GAMMA_MARKETS_URL, params=params,
                               timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                log.warning("Gamma API returned %d for slug=%s", resp.status, slug)
                return None
            data = await resp.json()

            if not data:
                log.debug("No market found for slug=%s", slug)
                return None

            top = data[0] if isinstance(data, list) else data

            # ── Collect all token objects from wherever they live ─────────────
            all_tokens = _extract_all_tokens(top)
            condition_id = top.get("conditionId") or top.get("condition_id") or ""
            end_date = top.get("endDateIso") or top.get("endDate") or top.get("end_date_iso") or ""

            # If top-level has no useful tokens, dig into sub-markets
            if len(all_tokens) < 2:
                sub_markets = top.get("markets", [])
                for sm in sub_markets:
                    all_tokens.extend(_extract_all_tokens(sm))
                    if not condition_id:
                        condition_id = sm.get("conditionId") or sm.get("condition_id") or ""
                    if not end_date:
                        end_date = sm.get("endDateIso") or sm.get("endDate") or ""

            if len(all_tokens) < 2:
                log.warning("Market %s: only %d token(s) found — skipping. "
                            "Top-level keys: %s", slug, len(all_tokens), list(top.keys()))
                return None

            # ── Identify UP / DOWN ────────────────────────────────────────────
            up_token_id, down_token_id = "", ""
            up_price, down_price = 0.50, 0.50

            for tok in all_tokens:
                outcome = (tok.get("outcome") or "").strip().upper()
                tid = tok.get("tokenId") or tok.get("token_id") or ""
                try:
                    price = float(tok.get("price") or 0.50)
                except (TypeError, ValueError):
                    price = 0.50

                if "UP" in outcome or outcome in ("YES", "HIGHER", "ABOVE"):
                    up_token_id, up_price = tid, price
                elif "DOWN" in outcome or outcome in ("NO", "LOWER", "BELOW"):
                    down_token_id, down_price = tid, price

            if not up_token_id or not down_token_id:
                # Positional fallback: first token = UP, second = DOWN
                tok0, tok1 = all_tokens[0], all_tokens[1]
                if not up_token_id:
                    up_token_id = tok0.get("tokenId") or tok0.get("token_id") or ""
                    try:
                        up_price = float(tok0.get("price") or 0.50)
                    except (TypeError, ValueError):
                        up_price = 0.50
                if not down_token_id:
                    up_token_id = up_token_id or (tok0.get("tokenId") or tok0.get("token_id") or "")
                    down_token_id = tok1.get("tokenId") or tok1.get("token_id") or ""
                    try:
                        down_price = float(tok1.get("price") or 0.50)
                    except (TypeError, ValueError):
                        down_price = 0.50
                log.debug("Used positional fallback for token IDs on %s", slug)

            if not up_token_id or not down_token_id:
                log.warning("Could not extract token IDs for %s", slug)
                return None

            # ── Parse close timestamp ─────────────────────────────────────────
            close_ts = _parse_end_date(end_date, slug)
            open_ts = close_ts - config.WINDOW_INTERVAL

            log.debug("Parsed market %s | UP=%s DOWN=%s close_ts=%d",
                      slug, up_token_id[:8], down_token_id[:8], close_ts)

            return MarketInfo(
                slug=slug,
                condition_id=condition_id,
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_all_tokens(market: dict) -> list:
    """Return all token objects from a market dict.

    Handles three Gamma API shapes:
      Shape A — tokens[]: [{tokenId/token_id, outcome, price}, ...]
      Shape B — clobTokenIds[]: paired with outcomes[] and outcomePrices[]
      Shape C — empty / missing tokens (caller will dig into sub-markets)
    """
    # Shape A: explicit tokens array with objects
    tokens = market.get("tokens") or []
    if isinstance(tokens, list) and tokens:
        return list(tokens)

    # Shape B: clobTokenIds paired with outcomes / outcomePrices
    # Gamma API returns these as JSON-encoded strings, not parsed lists
    clob_ids = market.get("clobTokenIds") or []
    outcomes = market.get("outcomes") or []
    prices = market.get("outcomePrices") or []
    if isinstance(clob_ids, str):
        clob_ids = json.loads(clob_ids)
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(prices, str):
        prices = json.loads(prices)
    if isinstance(clob_ids, list) and clob_ids:
        result = []
        for i, tid in enumerate(clob_ids):
            outcome = outcomes[i] if i < len(outcomes) else ""
            price = prices[i] if i < len(prices) else "0.50"
            result.append({"tokenId": tid, "outcome": outcome, "price": price})
        return result

    return []


def _parse_end_date(end_date: str, slug: str = "") -> int:
    """Parse ISO end date to unix timestamp.

    The Gamma API sometimes returns endDate as a bare date ("2026-03-21") with no
    time component, which fromisoformat() parses to midnight UTC — far in the past
    relative to intra-day 5-minute windows.  When that happens (seconds == 0), fall
    back to deriving the close time from the slug, which encodes the exact window-open
    timestamp as its trailing integer.  If the slug is unavailable, use the next
    computed window boundary.
    """
    import datetime
    if end_date:
        try:
            dt = datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            ts = int(dt.timestamp())
            # Date-only strings parse to midnight; treat as unusable
            if dt.hour != 0 or dt.minute != 0 or dt.second != 0:
                return ts
        except Exception:
            pass

    # Try to extract close time from the slug (format: btc-updown-5m-<open_ts>)
    if slug:
        try:
            parts = slug.rsplit("-", 1)
            open_ts = int(parts[-1])
            window_type = slug.split("-")[2]  # "5m" or "15m"
            interval = config.WINDOW_SECONDS.get(window_type, config.WINDOW_INTERVAL)
            return open_ts + interval
        except Exception:
            pass

    interval = config.WINDOW_INTERVAL
    now_ts = int(time.time())
    return now_ts - (now_ts % interval) + interval


async def get_current_market(window_type: str = "5m") -> Optional[MarketInfo]:
    """Return the currently active BTC up/down market."""
    async with aiohttp.ClientSession() as session:
        # Try current window; if not found, try next
        for offset in (0, 1, -1):
            slug = build_slug(window_type, offset)
            market = await fetch_market(slug, session)
            if market and market.is_active():
                log.info("Found market: %s (%.0fs remaining)", slug, market.seconds_remaining())
                return market
            if market:
                log.debug("Skipping expired market: %s", slug)
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
