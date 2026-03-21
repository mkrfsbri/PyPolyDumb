"""
Performance metrics computation.

Computed on-the-fly from trade history in SQLite:
  - Win rate (overall and per strategy)
  - Total PnL, daily PnL
  - Sharpe ratio (annualized)
  - Max drawdown
  - Trades per day
"""

import logging
import math
from typing import Optional

from analytics.trade_logger import TradeLogger

log = logging.getLogger(__name__)


class PerformanceTracker:
    def __init__(self, logger: TradeLogger):
        self._logger = logger

    def overall_stats(self) -> dict:
        trades = self._logger.recent_trades(n=10_000)
        resolved = [t for t in trades if t["outcome"] in ("WIN", "LOSS")]
        return self._compute_stats(resolved, label="overall")

    def strategy_stats(self, strategy: str) -> dict:
        trades = self._logger.trades_by_strategy(strategy)
        resolved = [t for t in trades if t["outcome"] in ("WIN", "LOSS")]
        return self._compute_stats(resolved, label=strategy)

    def all_strategy_stats(self) -> dict[str, dict]:
        trades = self._logger.recent_trades(n=10_000)
        strategies = {t["strategy"] for t in trades}
        result = {}
        for s in strategies:
            s_trades = [t for t in trades if t["strategy"] == s and t["outcome"] in ("WIN", "LOSS")]
            result[s] = self._compute_stats(s_trades, label=s)
        return result

    def pnl_curve(self, n: int = 200) -> list[dict]:
        """Return bankroll history for charting."""
        return self._logger.bankroll_history(n)

    def daily_summary(self) -> dict:
        import datetime
        today = datetime.datetime.utcnow().date().isoformat()
        trades = self._logger.recent_trades(n=500)
        today_trades = [t for t in trades if t["timestamp"].startswith(today)]
        resolved = [t for t in today_trades if t["outcome"] in ("WIN", "LOSS")]
        return self._compute_stats(resolved, label="today")

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_stats(trades: list[dict], label: str = "") -> dict:
        if not trades:
            return {
                "label": label, "total": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                "sharpe": 0.0, "max_drawdown": 0.0, "avg_confidence": 0.0,
            }

        wins = sum(1 for t in trades if t["outcome"] == "WIN")
        losses = len(trades) - wins
        total_pnl = sum(t["pnl"] or 0.0 for t in trades)
        pnl_list = [t["pnl"] or 0.0 for t in trades]
        avg_pnl = total_pnl / len(trades)
        confidences = [t["confidence"] or 0.0 for t in trades]

        sharpe = _sharpe(pnl_list)
        max_dd = _max_drawdown(pnl_list)

        return {
            "label": label,
            "total": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / len(trades),
            "total_pnl": round(total_pnl, 4),
            "avg_pnl": round(avg_pnl, 4),
            "sharpe": round(sharpe, 3),
            "max_drawdown": round(max_dd, 4),
            "avg_confidence": round(sum(confidences) / len(confidences), 3),
        }


def _sharpe(pnl_list: list[float], risk_free: float = 0.0) -> float:
    if len(pnl_list) < 2:
        return 0.0
    import statistics
    mean = statistics.mean(pnl_list) - risk_free
    std = statistics.stdev(pnl_list)
    if std == 0:
        return 0.0
    # Annualize: ~288 five-minute windows per day * 365
    annual_factor = math.sqrt(288 * 365)
    return (mean / std) * annual_factor


def _max_drawdown(pnl_list: list[float]) -> float:
    """Compute max drawdown from cumulative PnL series."""
    if not pnl_list:
        return 0.0
    cumulative = []
    running = 0.0
    for p in pnl_list:
        running += p
        cumulative.append(running)

    max_dd = 0.0
    peak = cumulative[0]
    for v in cumulative:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return max_dd
