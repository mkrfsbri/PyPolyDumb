"""
PyPolyDumb — Polymarket BTC Up/Down Multi-Strategy Trading Bot

Usage:
    python main.py [--strategies NAMES] [--mode MODE] [--market TYPE]
    python main.py --backtest --strategy NAME [--days N]
    python main.py --dashboard

Examples:
    python main.py --strategies endcycle_sniper,pair_cost_avg
    python main.py --strategies all --mode dry_run
    python main.py --strategies flash_crash --market 15m
    python main.py --backtest --strategy endcycle_sniper --days 30
    python main.py --dashboard
"""

import argparse
import asyncio
import logging
import sys
import time
from typing import Optional

import uvicorn

import config
from config import BOT_MODE, MARKET_TYPE, ACTIVE_STRATEGIES

log = logging.getLogger(__name__)


# ── Strategy registry ─────────────────────────────────────────────────────────

STRATEGY_MAP = {
    "endcycle_sniper": "strategies.endcycle_sniper:EndCycleSniper",
    "pair_cost_avg":   "strategies.pair_cost_avg:PairCostAvg",
    "latency_arb":     "strategies.latency_arb:LatencyArb",
    "flash_crash":     "strategies.flash_crash:FlashCrash",
    "market_maker":    "strategies.market_maker:MarketMaker",
    "momentum_cascade":"strategies.momentum_cascade:MomentumCascade",
    "monte_carlo":     "strategies.monte_carlo:MonteCarlo",
}


def load_strategy(name: str):
    spec = STRATEGY_MAP.get(name)
    if not spec:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(STRATEGY_MAP)}")
    module_path, cls_name = spec.rsplit(":", 1)
    import importlib
    module = importlib.import_module(module_path)
    cls = getattr(module, cls_name)
    return cls()


def load_strategies(names: list[str]):
    if names == ["all"]:
        names = list(STRATEGY_MAP.keys())
    strategies = []
    for name in names:
        try:
            s = load_strategy(name)
            strategies.append(s)
            log.info("Loaded strategy: %s", name)
        except Exception as e:
            log.error("Failed to load strategy %s: %s", name, e)
    return strategies


# ── Bot orchestrator ──────────────────────────────────────────────────────────

class BotOrchestrator:
    """
    Central coordinator that:
    1. Runs data feeds (Binance WS, Polymarket WS)
    2. Tracks the current market window
    3. Evaluates each strategy every 1-2 seconds
    4. Places/manages orders via OrderManager
    5. Tracks P&L and logs to SQLite
    6. Enforces risk limits and circuit breaker
    """

    def __init__(self, strategies: list, market_type: str, mode: str):
        from core.polymarket_client import PolymarketClient
        from core.market_discovery import MarketWatcher
        from core.websocket_manager import PolymarketWebSocket
        from core.order_manager import OrderManager
        from core.position_tracker import PositionTracker
        from feeds.binance_ws import BinanceFeed
        from feeds.chainlink_feed import ChainlinkFeed
        from feeds.orderbook_feed import OrderbookFeed
        from risk.bankroll_manager import BankrollManager
        from risk.risk_limits import RiskLimits
        from risk.drawdown_guard import DrawdownGuard
        from analytics.trade_logger import TradeLogger, TradeRecord
        from analytics.performance import PerformanceTracker

        self.strategies = strategies
        self.market_type = market_type
        self.mode = mode

        # Core components
        self.client = PolymarketClient()
        self.watcher = MarketWatcher(market_type)
        self.poly_ws = PolymarketWebSocket()
        self.order_mgr = OrderManager(self.client)
        self.tracker = PositionTracker()
        self.binance_feed = BinanceFeed()
        self.chainlink_feed = ChainlinkFeed()
        self.orderbook_feed = OrderbookFeed(self.poly_ws)

        # Risk
        self.bankroll = BankrollManager(config.STARTING_BANKROLL)
        self.risk = RiskLimits(self.bankroll, self.tracker)
        self.guard = DrawdownGuard()

        # Analytics
        self.logger = TradeLogger(config.DB_PATH)
        self.performance = PerformanceTracker(self.logger)

        # State
        self._current_market = None
        self._eval_interval = 1.0   # seconds between strategy evaluations
        self._running = False
        self._TradeRecord = TradeRecord
        self._bg_tasks: set = set()  # keep strong references to background tasks

    def _create_task(self, coro):
        """Create a tracked background task (prevents GC before completion)."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def run(self):
        """Main bot loop."""
        self._running = True
        log.info("Bot starting | mode=%s market=%s strategies=%s",
                 self.mode, self.market_type, [s.NAME for s in self.strategies])

        # Initialize client
        self.client.initialize()

        # Wire up market watcher callback
        self.watcher.on_new_window(self._on_new_window)

        # Wire up position P&L callback
        self.tracker.on_pnl_update(self._on_pnl_update)

        # Wire up fill detection
        self.order_mgr.on_fill(self._on_fill)

        # Start all background tasks
        tasks = [
            asyncio.create_task(self.binance_feed.run(), name="binance_ws"),
            asyncio.create_task(self.poly_ws.run(), name="poly_ws"),
            asyncio.create_task(self.watcher.run(), name="market_watcher"),
            asyncio.create_task(self.order_mgr.run(), name="order_manager"),
            asyncio.create_task(self.tracker.run(), name="position_tracker"),
            asyncio.create_task(self.chainlink_feed.run(), name="chainlink"),
            asyncio.create_task(self._eval_loop(), name="eval_loop"),
            asyncio.create_task(self._bankroll_log_loop(), name="bankroll_log"),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("Bot tasks cancelled")
        except Exception as e:
            log.error("Fatal bot error: %s", e)
            raise

    async def _eval_loop(self):
        """Evaluate all strategies every eval_interval seconds."""
        log.info("Evaluation loop started")
        while self._running:
            try:
                market = self.watcher.current
                if market and self.binance_feed.current_price > 0:
                    state = self._build_state(market)
                    await self._evaluate_all(state)
            except Exception as e:
                log.error("Eval loop error: %s", e)
            await asyncio.sleep(self._eval_interval)

    async def _evaluate_all(self, state):
        """Run each strategy's analyze() and act on signals."""
        from strategies.base_strategy import MarketState

        # Check circuit breaker
        can_trade, reason = self.guard.can_trade()
        if not can_trade:
            log.debug("Circuit breaker: %s", reason)
            return

        # Broadcast price update to dashboard
        try:
            from dashboard.server import broadcast_price
            self._create_task(broadcast_price(
                state.btc_price, state.window_delta, state.seconds_remaining
            ))
        except Exception:
            pass

        for strategy in self.strategies:
            if not strategy.is_enabled:
                continue
            try:
                signal = await strategy.analyze(state)
                if strategy.NAME == "market_maker":
                    if strategy.should_trade(signal, state):
                        await self._place_mm_quotes(strategy, signal, state)
                elif signal.is_actionable() and strategy.should_trade(signal, state):
                    await self._place_trade(strategy, signal, state)
            except Exception as e:
                log.error("Strategy %s error: %s", strategy.NAME, e)

    async def _place_trade(self, strategy, signal, state):
        """Size, validate, and place a trade."""
        from core.position_tracker import Position
        from analytics.trade_logger import TradeRecord

        market = state.market

        # Determine token ID and size
        is_up = signal.direction == "UP"
        token_id = market.up_token_id if is_up else market.down_token_id

        # Size via half-Kelly
        size = strategy.calculate_size(
            win_prob=signal.confidence,
            bankroll=self.bankroll.bankroll,
            entry_price=signal.suggested_price,
            size_multiplier=self.guard.size_multiplier,
        )

        # Fixed-share strategies bypass Kelly.
        # size = MAX_SHARES_PER_LEG × price  (e.g. 10 shares × $0.40 = $4.00 cost)
        # Apply BEFORE the size <= 0 guard so they aren't skipped.
        if strategy.NAME in ("pair_cost_avg", "endcycle_sniper"):
            size = min(
                config.MAX_SHARES_PER_LEG * signal.suggested_price,
                self.bankroll.available,
            )
        elif size <= 0:
            log.debug("Zero size for %s — skipping", strategy.NAME)
            return

        # Risk limit check
        ok, reason = self.risk.check(size, strategy.NAME, market.slug)
        if not ok:
            log.warning("Risk limit rejected %s trade: %s", strategy.NAME, reason)
            return

        log.info("Placing %s %s order | strategy=%s price=%.3f size=$%.2f conf=%.2f",
                 signal.direction, market.slug, strategy.NAME,
                 signal.suggested_price, size, signal.confidence)

        # Place order — use per-signal TTL if set, else fall back to strategy default
        cancel_secs = (
            signal.cancel_after_secs
            if signal.cancel_after_secs is not None
            else self._cancel_secs(strategy.NAME)
        )
        order = await self.order_mgr.place_order(
            token_id=token_id,
            side="BUY",
            price=signal.suggested_price,
            size=size,
            strategy=strategy.NAME,
            cancel_after_secs=cancel_secs,
            fee_rate_bps=0,   # maker = zero fee
            neg_risk=market.neg_risk,  # from Gamma API — correct exchange for signing
        )

        if not order:
            return

        # Allocate capital
        self.bankroll.allocate(size)

        # Log trade
        shares = size / signal.suggested_price
        record = TradeRecord(
            strategy=strategy.NAME,
            market_slug=market.slug,
            direction=signal.direction,
            entry_price=signal.suggested_price,
            size_usdc=size,
            shares=shares,
            window_delta=state.window_delta,
            confidence=signal.confidence,
            order_id=order.order_id,
        )
        trade_id = self.logger.log_trade(record)

        # Track position
        position = Position(
            window_slug=market.slug,
            token_id=token_id,
            direction=signal.direction,
            strategy=strategy.NAME,
            shares=shares,
            entry_price=signal.suggested_price,
            size_usdc=size,
            window_close_ts=market.window_close_ts,
        )
        self.tracker.add_position(position)

        # Notify strategy
        if strategy.NAME == "flash_crash" and hasattr(strategy, "record_leg1"):
            # Record leg1 so hedge check works; hedge is signalled on next analyze() call
            strategy.record_leg1(market.slug, signal.direction,
                                 signal.suggested_price, size, token_id)
        elif hasattr(strategy, "record_trade"):
            strategy.record_trade(market.slug)
        elif strategy.NAME == "pair_cost_avg" and hasattr(strategy, "record_order_placed"):
            # Pass price so Phase 2 combined cost check uses real leg1 price.
            strategy.record_order_placed(market.slug, signal.direction, signal.suggested_price)

        # Dashboard broadcast
        try:
            from dashboard.server import broadcast_trade
            self._create_task(broadcast_trade({
                "strategy": strategy.NAME,
                "direction": signal.direction,
                "price": signal.suggested_price,
                "size": size,
                "market": market.slug,
            }))
        except Exception:
            pass

    async def _place_mm_quotes(self, strategy, signal, state):
        """Place two-sided market maker quotes for UP and DOWN tokens."""
        from strategies.market_maker import MarketMaker
        market = state.market
        quotes = strategy.parse_quotes(signal)
        if not quotes:
            return

        mm_size = min(3.0, self.bankroll.available / 4)
        if mm_size < 1.0:
            return

        for direction, price in [
            ("UP", quotes.up_bid),
            ("DOWN", quotes.down_bid),
        ]:
            token_id = market.up_token_id if direction == "UP" else market.down_token_id
            ok, reason = self.risk.check(mm_size, "market_maker", market.slug)
            if not ok:
                continue
            order = await self.order_mgr.place_order(
                token_id=token_id,
                side="BUY",
                price=price,
                size=mm_size,
                strategy="market_maker",
                cancel_after_secs=config.MM_REFRESH_SECS * 1.5,
                fee_rate_bps=0,
                neg_risk=market.neg_risk,  # from Gamma API — correct exchange for signing
            )
            if order:
                self.bankroll.allocate(mm_size)
                log.info("MM quote placed: BUY %s @ %.3f size=$%.2f", direction, price, mm_size)

    async def _on_new_window(self, market):
        """Called when a new market window opens."""
        log.info("Window opened: %s | %s remaining",
                 market.slug, int(market.seconds_remaining()))

        # Subscribe Polymarket WS to new token pair
        self.poly_ws.subscribe(market.up_token_id, market.down_token_id)
        self._current_market = market

        # Clear per-window state in strategies
        for strategy in self.strategies:
            if hasattr(strategy, "clear_window"):
                strategy.clear_window(market.slug)

    async def _on_fill(self, order):
        """Called when an order is detected as filled."""
        log.info("Fill detected: %s | strategy=%s", order.order_id, order.strategy)
        # Capital stays allocated until settlement (position closes), not at fill time.

        # Notify strategies of fill
        market = self.watcher.current
        for strategy in self.strategies:
            if strategy.NAME == "pair_cost_avg" and hasattr(strategy, "record_fill"):
                if market:
                    direction = "UP" if order.token_id == market.up_token_id else "DOWN"
                    strategy.record_fill(market.slug, direction, order.price, order.size)
            elif strategy.NAME == "flash_crash" and hasattr(strategy, "record_hedge"):
                if market and order.strategy == "flash_crash":
                    direction = "UP" if order.token_id == market.up_token_id else "DOWN"
                    # If this fill is the hedge leg (opposite of the open leg), record it
                    if market.slug in strategy._open_legs:
                        leg = strategy._open_legs[market.slug]
                        if leg.direction != direction:
                            strategy.record_hedge(market.slug, order.price)

    async def _on_pnl_update(self, position, pnl: float):
        """Called when a position is settled."""
        # Release the capital that was locked for this position
        self.bankroll.deallocate(position.size_usdc)
        self.bankroll.realize_pnl(pnl)
        # PUSH (pnl == 0, outcome == "PUSH") is a forced expiry, not a real loss.
        if pnl > 0:
            self.guard.record_win()
        elif position.outcome != "PUSH":
            self.guard.record_loss()
        self.guard.check_daily_loss(self.tracker._current_daily_pnl())

        # Log to SQLite (find trade by order strategy+slug+direction)
        # Update pending trades for this window
        pending = self.logger.pending_trades()
        for t in pending:
            if (t["strategy"] == position.strategy
                    and t["market_slug"] == position.window_slug
                    and t["direction"] == position.direction):
                self.logger.update_trade_outcome(
                    t["id"], position.outcome, position.exit_price, pnl
                )
                break

        # Broadcast bankroll update
        try:
            from dashboard.server import broadcast_bankroll
            summary = self.tracker.summary()
            self._create_task(broadcast_bankroll(
                self.bankroll.bankroll, summary.get("daily_pnl", 0.0)
            ))
        except Exception:
            pass

        # Log bankroll snapshot
        summary = self.tracker.summary()
        self.logger.log_bankroll(
            self.bankroll.bankroll,
            summary.get("daily_pnl", 0.0),
            f"{position.strategy}/{position.outcome}",
        )

    async def _bankroll_log_loop(self):
        """Log bankroll every 5 minutes."""
        while self._running:
            await asyncio.sleep(300)
            try:
                summary = self.tracker.summary()
                self.logger.log_bankroll(
                    self.bankroll.bankroll,
                    summary.get("daily_pnl", 0.0),
                    "periodic",
                )
            except Exception as e:
                log.warning("Bankroll log error: %s", e)

    @staticmethod
    def _cancel_secs(strategy_name: str) -> Optional[float]:
        """Order auto-cancel timeout per strategy."""
        cancel_map = {
            "pair_cost_avg": 120.0,   # fallback — normally overridden by signal.cancel_after_secs
            "endcycle_sniper": 20.0,
            "latency_arb": config.LATENCY_CANCEL_SECS,
            "flash_crash": 30.0,
            "market_maker": config.MM_REFRESH_SECS * 1.5,
            "momentum_cascade": 25.0,
            "monte_carlo": 20.0,
        }
        return cancel_map.get(strategy_name)

    def _build_state(self, market):
        """Construct a MarketState snapshot from all live feeds."""
        from strategies.base_strategy import MarketState

        book_up = self.orderbook_feed.get_metrics(market.up_token_id)
        book_down = self.orderbook_feed.get_metrics(market.down_token_id)
        ind = self.binance_feed.indicators

        return MarketState(
            market=market,
            btc_price=self.binance_feed.current_price,
            window_delta=self.binance_feed.window_delta,
            momentum_30s=self.binance_feed.momentum_30s(),
            ema9=ind.ema9,
            ema21=ind.ema21,
            rsi=ind.rsi,
            macd=ind.macd,
            macd_signal=ind.macd_signal,
            bb_upper=ind.bb_upper,
            bb_lower=ind.bb_lower,
            bb_mid=ind.bb_mid,
            volume_ratio=ind.volume_ratio,
            up_price=book_up.mid_price if book_up else market.up_price,
            down_price=book_down.mid_price if book_down else market.down_price,
            up_bid=book_up.best_bid if book_up else 0.0,
            up_ask=book_up.best_ask if book_up else market.up_price,
            down_bid=book_down.best_bid if book_down else 0.0,
            down_ask=book_down.best_ask if book_down else market.down_price,
            orderflow_imbalance_up=self.orderbook_feed.orderflow_imbalance(market.up_token_id),
            orderflow_imbalance_down=self.orderbook_feed.orderflow_imbalance(market.down_token_id),
            btc_open=self.binance_feed.window_open,
            seconds_remaining=market.seconds_remaining(),
        )

    def stop(self):
        self._running = False
        self.binance_feed.stop()
        self.poly_ws.stop()
        self.watcher.stop()
        self.order_mgr.stop()
        self.tracker.stop()
        self.chainlink_feed.stop()


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="PyPolyDumb — Polymarket BTC Trading Bot")
    parser.add_argument("--strategies", default=",".join(ACTIVE_STRATEGIES),
                        help="Comma-separated strategy names or 'all'")
    parser.add_argument("--mode", choices=["dry_run", "paper", "live"],
                        default=BOT_MODE, help="Trading mode")
    parser.add_argument("--market", choices=["5m", "15m"], default=MARKET_TYPE,
                        help="Market window type")
    parser.add_argument("--backtest", action="store_true",
                        help="Run backtest instead of live bot")
    parser.add_argument("--strategy", default="endcycle_sniper",
                        help="Strategy to backtest (with --backtest)")
    parser.add_argument("--days", type=int, default=7,
                        help="Days of history for backtest")
    parser.add_argument("--dashboard", action="store_true",
                        help="Start only the dashboard server")
    return parser.parse_args()


def setup_logging(level: str = config.LOG_LEVEL):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)-20s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)


async def run_bot(args):
    """Start the full trading bot."""
    strategy_names = [s.strip() for s in args.strategies.split(",")]
    strategies = load_strategies(strategy_names)
    if not strategies:
        log.error("No strategies loaded — exiting")
        return

    # Override config from CLI args
    import config as cfg
    cfg.BOT_MODE = args.mode
    cfg.MARKET_TYPE = args.market
    cfg.WINDOW_INTERVAL = cfg.WINDOW_SECONDS.get(args.market, 300)

    if args.mode == "live" and not config.is_live_mode():
        log.error("Live mode requires CONFIRM_LIVE=yes in .env")
        return

    bot = BotOrchestrator(strategies, args.market, args.mode)

    # Inject into dashboard
    from dashboard import server as dash
    dash.register("mode", args.mode)
    dash.register("active_strategies", strategy_names)
    dash.register("bankroll", bot.bankroll)
    dash.register("tracker", bot.tracker)
    dash.register("logger", bot.logger)
    dash.register("performance", bot.performance)
    dash.register("guard", bot.guard)
    dash.register("binance_feed", bot.binance_feed)
    dash.register("watcher", bot.watcher)

    # Start dashboard alongside bot
    dash_config = uvicorn.Config(
        dash.app,
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        log_level="warning",
    )
    dash_server = uvicorn.Server(dash_config)

    log.info("Dashboard at http://%s:%d", config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    log.info("Bot mode: %s | Market: %s | Bankroll: $%.2f",
             args.mode, args.market, config.STARTING_BANKROLL)

    await asyncio.gather(
        bot.run(),
        dash_server.serve(),
    )


async def run_dashboard_only():
    """Start just the dashboard (read-only mode)."""
    from dashboard import server as dash
    dash_config = uvicorn.Config(
        dash.app,
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        log_level="info",
    )
    server = uvicorn.Server(dash_config)
    log.info("Dashboard-only mode at http://%s:%d", config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    await server.serve()


async def run_backtest(args):
    """Run historical backtest."""
    from analytics.backtester import run_backtest as do_backtest
    log.info("Backtesting strategy=%s days=%d market=%s",
             args.strategy, args.days, args.market)
    await do_backtest(args.strategy, args.days, args.market)


def main():
    args = parse_args()
    setup_logging()

    try:
        if args.backtest:
            asyncio.run(run_backtest(args))
        elif args.dashboard:
            asyncio.run(run_dashboard_only())
        else:
            asyncio.run(run_bot(args))
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
    except Exception as e:
        log.error("Fatal error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
