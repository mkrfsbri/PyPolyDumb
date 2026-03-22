"""
Abstract base class for all trading strategies.

Each strategy:
1. Receives a MarketState snapshot on every evaluation cycle
2. Returns a Signal with direction, confidence, and suggested order params
3. Decides should_trade() based on its own internal logic
4. Optionally manages its own open orders (for market-making style strategies)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import time

from core.market_discovery import MarketInfo


@dataclass
class MarketState:
    """Snapshot of all market data passed to each strategy on evaluation."""
    market: MarketInfo

    # BTC price data
    btc_price: float = 0.0
    window_delta: float = 0.0      # (current - open) / open
    momentum_30s: float = 0.0      # price change in last 30s

    # Technical indicators (from BinanceFeed)
    ema9: float = 0.0
    ema21: float = 0.0
    rsi: float = 50.0
    macd: float = 0.0
    macd_signal: float = 0.0
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_mid: float = 0.0
    volume_ratio: float = 1.0

    # Polymarket orderbook data
    up_price: float = 0.50
    down_price: float = 0.50
    up_bid: float = 0.0
    up_ask: float = 1.0
    down_bid: float = 0.0
    down_ask: float = 1.0
    orderflow_imbalance_up: float = 0.0
    orderflow_imbalance_down: float = 0.0

    # BTC window open price (for Monte Carlo)
    btc_open: float = 0.0

    # Timing
    seconds_remaining: float = 300.0
    timestamp: float = field(default_factory=time.time)

    def time_fraction(self) -> float:
        """Fraction of window elapsed (0=start, 1=end)."""
        import config
        total = float(config.WINDOW_INTERVAL)
        elapsed = total - self.seconds_remaining
        return min(1.0, max(0.0, elapsed / total))

    def is_end_cycle(self, activation_secs: int = 30, deactivate_secs: int = 10) -> bool:
        """True if we're in the end-cycle sniper window."""
        return deactivate_secs <= self.seconds_remaining <= activation_secs


@dataclass
class Signal:
    direction: str = "NEUTRAL"                  # UP | DOWN | NEUTRAL
    confidence: float = 0.0                     # 0.0 to 1.0
    suggested_price: float = 0.50               # maker order price
    suggested_size: float = 0.0                 # USDC to spend
    reason: str = ""                            # human-readable explanation
    cancel_after_secs: Optional[float] = None   # override default TTL per-signal

    def is_actionable(self) -> bool:
        """True if this signal should lead to a trade.
        suggested_size=0 is valid — orchestrator determines size via Kelly.
        """
        return self.direction != "NEUTRAL"


class BaseStrategy(ABC):
    """Abstract base for all strategies."""

    NAME: str = "base"

    def __init__(self):
        self._enabled: bool = True
        self._trade_count: int = 0
        self._win_count: int = 0
        self._loss_count: int = 0

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    @abstractmethod
    async def analyze(self, state: MarketState) -> Signal:
        """
        Core analysis. Called every evaluation cycle (typically every 1-2s).
        Must be non-blocking and fast (<50ms).
        """
        ...

    @abstractmethod
    def should_trade(self, signal: Signal, state: MarketState) -> bool:
        """Additional guard: return True only if we should actually place an order."""
        ...

    def calculate_size(
        self,
        win_prob: float,
        bankroll: float,
        entry_price: float,
        size_multiplier: float = 1.0,
    ) -> float:
        """
        Half-Kelly position sizing.
        payout_ratio = net profit per unit risked = (1 - entry_price) / entry_price
        """
        from risk.bankroll_manager import BankrollManager, MAX_ABS_BET
        import config

        if entry_price <= 0 or entry_price >= 1:
            return 0.0

        payout = (1.0 - entry_price) / entry_price
        edge = win_prob * payout - (1.0 - win_prob)
        if edge <= 0:
            return 0.0

        kelly_pct = (edge / payout) * 0.5  # half-Kelly
        raw_size = bankroll * kelly_pct * size_multiplier
        capped = min(raw_size, bankroll * config.MAX_BET_FRACTION, MAX_ABS_BET)
        return round(max(0.0, capped), 2)

    def record_outcome(self, won: bool):
        self._trade_count += 1
        if won:
            self._win_count += 1
        else:
            self._loss_count += 1

    def stats(self) -> dict:
        total = self._trade_count
        return {
            "strategy": self.NAME,
            "trades": total,
            "wins": self._win_count,
            "losses": self._loss_count,
            "win_rate": self._win_count / total if total else 0.0,
        }
