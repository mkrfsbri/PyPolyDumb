"""
Binance WebSocket price feed for BTC/USDT.

Subscribes to:
  - btcusdt@trade       : real-time tick price
  - btcusdt@kline_1m    : 1-minute OHLCV candles

Maintains:
  - window_open_price   : price at start of current 5m/15m window
  - window_delta        : (current - open) / open
  - Rolling candle buffers (1m, 5m, 15m)
  - Real-time EMA(9), EMA(21), RSI(14), MACD(12/26/9), BB(20)
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import websockets

import config

log = logging.getLogger(__name__)

BINANCE_WS_URL = f"{config.BINANCE_WS}?streams={config.BINANCE_STREAMS}"


@dataclass
class Candle:
    open_time: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = False


@dataclass
class Indicators:
    ema9: float = 0.0
    ema21: float = 0.0
    rsi: float = 50.0
    macd: float = 0.0
    macd_signal: float = 0.0
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_mid: float = 0.0
    volume_ratio: float = 1.0  # current vol / avg vol


class BinanceFeed:
    """
    Real-time BTC/USDT price feed with indicator computation.
    All data is updated in-place; strategies read via properties.
    """

    def __init__(self):
        self._current_price: float = 0.0
        self._window_open: float = 0.0
        self._window_type: str = config.MARKET_TYPE
        self._window_interval: int = config.WINDOW_INTERVAL
        self._next_window_ts: float = 0.0

        # Candle buffers
        self._candles_1m: deque = deque(maxlen=200)
        self._current_candle: Optional[Candle] = None

        # Computed indicators
        self._indicators = Indicators()
        self._price_history_30s: deque = deque(maxlen=60)  # tick prices last 30s (0.5s sampling)

        self._running = False
        self._connected = False
        self._update_callbacks: list = []

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def current_price(self) -> float:
        return self._current_price

    @property
    def window_open(self) -> float:
        return self._window_open

    @property
    def window_delta(self) -> float:
        """(current - open) / open  as a fraction (e.g. 0.001 = 0.1%)"""
        if self._window_open <= 0:
            return 0.0
        return (self._current_price - self._window_open) / self._window_open

    @property
    def window_delta_pct(self) -> float:
        """window_delta in percent"""
        return self.window_delta * 100.0

    @property
    def indicators(self) -> Indicators:
        return self._indicators

    @property
    def is_connected(self) -> bool:
        return self._connected

    def seconds_in_window(self) -> float:
        """Seconds elapsed since window start."""
        now = time.time()
        interval = self._window_interval
        window_start = now - (now % interval)
        return now - window_start

    def seconds_until_close(self) -> float:
        """Seconds remaining in current window."""
        return self._window_interval - self.seconds_in_window()

    def momentum_30s(self) -> float:
        """Price change over the last 30 seconds as a fraction."""
        if len(self._price_history_30s) < 2:
            return 0.0
        old_price = self._price_history_30s[0]
        if old_price <= 0:
            return 0.0
        return (self._current_price - old_price) / old_price

    def on_update(self, callback):
        """Register callback called on every price tick: def callback(price, delta)"""
        self._update_callbacks.append(callback)

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        """Connect to Binance WS with exponential backoff reconnect."""
        self._running = True
        self._init_window_open()
        backoff = 2

        while self._running:
            try:
                async with websockets.connect(BINANCE_WS_URL) as ws:
                    self._connected = True
                    backoff = 2
                    log.info("Binance WS connected")
                    await self._receive_loop(ws)
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                self._connected = False
                log.warning("Binance WS disconnected: %s — retry in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                self._connected = False
                log.error("Binance WS error: %s — retry in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _receive_loop(self, ws):
        async for raw in ws:
            if not self._running:
                break
            try:
                data = json.loads(raw)
                stream = data.get("stream", "")
                payload = data.get("data", data)

                if "trade" in stream or payload.get("e") == "trade":
                    self._handle_trade(payload)
                elif "kline" in stream or payload.get("e") == "kline":
                    self._handle_kline(payload)
            except Exception as e:
                log.warning("Binance message error: %s", e)

    def _handle_trade(self, payload: dict):
        price = float(payload.get("p", 0) or payload.get("price", 0))
        if price <= 0:
            return

        self._current_price = price
        self._price_history_30s.append(price)
        self._check_window_rollover()

        for cb in self._update_callbacks:
            try:
                cb(price, self.window_delta)
            except Exception:
                pass

    def _handle_kline(self, payload: dict):
        k = payload.get("k", payload)
        candle = Candle(
            open_time=float(k.get("t", 0)) / 1000.0,
            open=float(k.get("o", 0)),
            high=float(k.get("h", 0)),
            low=float(k.get("l", 0)),
            close=float(k.get("c", 0)),
            volume=float(k.get("v", 0)),
            closed=k.get("x", False),
        )
        self._current_candle = candle

        if candle.closed:
            self._candles_1m.append(candle)
            self._compute_indicators()

    def _check_window_rollover(self):
        """Reset window_open when a new 5m/15m window starts."""
        now = time.time()
        interval = self._window_interval
        window_start = now - (now % interval)

        if window_start > self._next_window_ts - interval:
            if self._window_open == 0.0 or window_start >= self._next_window_ts:
                self._window_open = self._current_price
                self._next_window_ts = window_start + interval
                log.debug("New window opened. BTC open price: %.2f", self._window_open)

    def _init_window_open(self):
        """Bootstrap window open time tracking."""
        now = time.time()
        interval = self._window_interval
        window_start = now - (now % interval)
        self._next_window_ts = window_start + interval
        # window_open will be set on first trade tick

    # ── Indicator computation ─────────────────────────────────────────────────

    def _compute_indicators(self):
        closes = [c.close for c in self._candles_1m]
        volumes = [c.volume for c in self._candles_1m]
        if len(closes) < 26:
            return

        closes_arr = np.array(closes, dtype=float)
        vols_arr = np.array(volumes, dtype=float)

        ind = self._indicators

        # EMA
        ind.ema9 = float(self._ema(closes_arr, 9)[-1])
        ind.ema21 = float(self._ema(closes_arr, 21)[-1])

        # RSI(14)
        ind.rsi = float(self._rsi(closes_arr, 14))

        # MACD(12, 26, 9)
        ema12 = self._ema(closes_arr, 12)
        ema26 = self._ema(closes_arr, 26)
        macd_line = ema12 - ema26
        signal = self._ema(macd_line[25:], 9)
        ind.macd = float(macd_line[-1])
        ind.macd_signal = float(signal[-1]) if len(signal) else 0.0

        # Bollinger Bands(20)
        if len(closes_arr) >= 20:
            window = closes_arr[-20:]
            mid = np.mean(window)
            std = np.std(window)
            ind.bb_mid = float(mid)
            ind.bb_upper = float(mid + 2 * std)
            ind.bb_lower = float(mid - 2 * std)

        # Volume ratio
        if len(vols_arr) >= 20:
            avg_vol = float(np.mean(vols_arr[-20:-1]))
            ind.volume_ratio = float(vols_arr[-1]) / avg_vol if avg_vol > 0 else 1.0

    @staticmethod
    def _ema(data: np.ndarray, period: int) -> np.ndarray:
        k = 2.0 / (period + 1)
        ema = np.zeros_like(data)
        ema[0] = data[0]
        for i in range(1, len(data)):
            ema[i] = data[i] * k + ema[i - 1] * (1 - k)
        return ema

    @staticmethod
    def _rsi(data: np.ndarray, period: int = 14) -> float:
        deltas = np.diff(data[-period - 1:])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1 + rs))

    def stop(self):
        self._running = False
