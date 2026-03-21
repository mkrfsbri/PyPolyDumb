"""
Historical backtester — simulates strategies on past BTC 1m candle data.

Workflow:
1. Fetch historical 1m BTC/USDT candles from Binance REST API
2. Group candles into 5m / 15m windows
3. For each window, run each strategy's analyze() with simulated state
4. Record simulated trades and compute performance metrics
5. Print + optionally export CSV report

Usage:
    python main.py --backtest --strategy endcycle_sniper --days 30
"""

import asyncio
import csv
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import numpy as np

import config

log = logging.getLogger(__name__)

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


@dataclass
class HistoricalCandle:
    open_time: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class WindowSim:
    """Simulated 5m/15m window for backtesting."""
    open_ts: float
    close_ts: float
    open_price: float
    close_price: float
    direction: str          # UP | DOWN
    candles: list = field(default_factory=list)  # 1m candles in this window

    def window_delta(self) -> float:
        if self.open_price <= 0:
            return 0.0
        return (self.close_price - self.open_price) / self.open_price


async def fetch_candles(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    days: int = 7,
    session: Optional[aiohttp.ClientSession] = None,
) -> list[HistoricalCandle]:
    """Fetch historical 1m candles from Binance REST API."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    limit = 1000
    all_candles: list[HistoricalCandle] = []

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        current_start = start_ms
        while current_start < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": current_start,
                "endTime": end_ms,
                "limit": limit,
            }
            async with session.get(BINANCE_KLINES_URL, params=params) as resp:
                if resp.status != 200:
                    log.error("Binance API error %d", resp.status)
                    break
                data = await resp.json()
                if not data:
                    break

                for row in data:
                    all_candles.append(HistoricalCandle(
                        open_time=float(row[0]) / 1000.0,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    ))

                last_ts = int(data[-1][0])
                if last_ts <= current_start:
                    break
                current_start = last_ts + 60_000  # next minute

    finally:
        if close_session:
            await session.close()

    log.info("Fetched %d 1m candles (%d days)", len(all_candles), days)
    return all_candles


def build_windows(candles: list[HistoricalCandle], window_type: str = "5m") -> list[WindowSim]:
    """Group 1m candles into 5m or 15m windows."""
    interval = config.WINDOW_SECONDS.get(window_type, 300)
    windows: list[WindowSim] = []
    bucket: dict[int, list[HistoricalCandle]] = {}

    for candle in candles:
        window_ts = int(candle.open_time) - (int(candle.open_time) % interval)
        bucket.setdefault(window_ts, []).append(candle)

    for ts in sorted(bucket.keys()):
        window_candles = bucket[ts]
        if not window_candles:
            continue
        open_price = window_candles[0].open
        close_price = window_candles[-1].close
        direction = "UP" if close_price >= open_price else "DOWN"
        windows.append(WindowSim(
            open_ts=float(ts),
            close_ts=float(ts + interval),
            open_price=open_price,
            close_price=close_price,
            direction=direction,
            candles=window_candles,
        ))

    log.info("Built %d %s windows", len(windows), window_type)
    return windows


@dataclass
class BacktestResult:
    strategy: str
    total_windows: int
    trades_placed: int
    wins: int
    losses: int
    total_pnl: float
    win_rate: float
    avg_pnl_per_trade: float
    max_drawdown: float


def run_endcycle_sniper_backtest(windows: list[WindowSim]) -> BacktestResult:
    """Simulate End-Cycle Sniper strategy on historical windows."""
    from config import estimate_token_price

    wins = losses = trades = 0
    pnl_series = []
    bankroll = config.STARTING_BANKROLL

    for w in windows:
        delta = w.window_delta()

        # Confidence check
        abs_delta = abs(delta)
        if abs_delta < 0.0002:
            continue  # coin flip, skip

        if abs_delta >= 0.001:
            confidence = 0.85
        elif abs_delta >= 0.0005:
            confidence = 0.70
        else:
            confidence = 0.58

        # Entry price based on delta
        entry_price = estimate_token_price(delta)
        if entry_price > 0.95:
            continue  # too expensive, no edge

        # Bet size: 5% of bankroll
        size = min(bankroll * 0.05, 10.0)
        shares = size / entry_price
        win_pnl = shares * (1.0 - entry_price)
        loss_pnl = -size

        # Determine if bet is correct
        direction = "UP" if delta > 0 else "DOWN"
        won = direction == w.direction

        trades += 1
        if won:
            wins += 1
            bankroll += win_pnl
            pnl_series.append(win_pnl)
        else:
            losses += 1
            bankroll += loss_pnl
            pnl_series.append(loss_pnl)

    total_pnl = sum(pnl_series)
    win_rate = wins / trades if trades else 0.0
    avg_pnl = total_pnl / trades if trades else 0.0
    max_dd = _max_drawdown_from_series(pnl_series)

    return BacktestResult(
        strategy="endcycle_sniper",
        total_windows=len(windows),
        trades_placed=trades,
        wins=wins,
        losses=losses,
        total_pnl=round(total_pnl, 4),
        win_rate=round(win_rate, 4),
        avg_pnl_per_trade=round(avg_pnl, 4),
        max_drawdown=round(max_dd, 4),
    )


def run_monte_carlo_backtest(windows: list[WindowSim]) -> BacktestResult:
    """Simulate Monte Carlo strategy on historical windows."""
    from config import MC_PATHS, MC_MIN_EDGE

    wins = losses = trades = 0
    pnl_series = []
    bankroll = config.STARTING_BANKROLL

    for w in windows:
        # Simulate at T-60s: use first 4 minutes of candles (T-60s point)
        if len(w.candles) < 4:
            continue

        sim_candles = w.candles[:4]
        current_price = sim_candles[-1].close
        closes = [c.close for c in sim_candles]
        if len(closes) < 2:
            continue

        # Estimate vol from 1m returns
        returns = np.diff(np.log(closes))
        vol = float(np.std(returns)) if len(returns) > 0 else 0.001

        # Monte Carlo simulation
        time_remaining = 60.0  # last 60 seconds
        dt = time_remaining / (252 * 24 * 3600)
        Z = np.random.standard_normal(MC_PATHS)
        S_end = current_price * np.exp((-vol**2 / 2) * dt + vol * np.sqrt(dt) * Z)
        p_up = float(np.mean(S_end >= w.open_price))

        market_implied = 0.50
        edge = abs(p_up - market_implied)

        if edge < MC_MIN_EDGE:
            continue

        direction = "UP" if p_up > 0.5 else "DOWN"
        entry_price = market_implied + (edge * 0.5)  # simplified
        size = min(bankroll * 0.05, 8.0)
        shares = size / entry_price
        win_pnl = shares * (1.0 - entry_price)
        loss_pnl = -size

        won = direction == w.direction
        trades += 1
        if won:
            wins += 1
            bankroll += win_pnl
            pnl_series.append(win_pnl)
        else:
            losses += 1
            bankroll += loss_pnl
            pnl_series.append(loss_pnl)

    total_pnl = sum(pnl_series)
    return BacktestResult(
        strategy="monte_carlo",
        total_windows=len(windows),
        trades_placed=trades,
        wins=wins,
        losses=losses,
        total_pnl=round(total_pnl, 4),
        win_rate=round(wins / trades, 4) if trades else 0.0,
        avg_pnl_per_trade=round(total_pnl / trades, 4) if trades else 0.0,
        max_drawdown=round(_max_drawdown_from_series(pnl_series), 4),
    )


def print_report(result: BacktestResult):
    print(f"\n{'='*50}")
    print(f"Backtest: {result.strategy}")
    print(f"{'='*50}")
    print(f"  Windows simulated : {result.total_windows}")
    print(f"  Trades placed     : {result.trades_placed}")
    print(f"  Wins              : {result.wins}")
    print(f"  Losses            : {result.losses}")
    print(f"  Win rate          : {result.win_rate:.1%}")
    print(f"  Total PnL         : ${result.total_pnl:+.4f}")
    print(f"  Avg PnL / trade   : ${result.avg_pnl_per_trade:+.4f}")
    print(f"  Max drawdown      : ${result.max_drawdown:.4f}")
    print()


def export_csv(result: BacktestResult, path: str):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=result.__dataclass_fields__.keys())
        writer.writeheader()
        writer.writerow(result.__dict__)
    log.info("Backtest results exported to %s", path)


async def run_backtest(strategy: str, days: int = 7, window_type: str = "5m"):
    candles = await fetch_candles(days=days)
    windows = build_windows(candles, window_type)

    if strategy == "endcycle_sniper" or strategy == "all":
        result = run_endcycle_sniper_backtest(windows)
        print_report(result)

    if strategy == "monte_carlo" or strategy == "all":
        result = run_monte_carlo_backtest(windows)
        print_report(result)


def _max_drawdown_from_series(pnl_list: list[float]) -> float:
    if not pnl_list:
        return 0.0
    peak = 0.0
    running = 0.0
    max_dd = 0.0
    for p in pnl_list:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    return max_dd
