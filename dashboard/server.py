"""
FastAPI dashboard server with WebSocket for live updates.

Endpoints:
  GET  /api/status        — bot mode, active strategies, circuit breaker state
  GET  /api/bankroll      — current bankroll, daily PnL, return %
  GET  /api/trades        — recent 50 trades
  GET  /api/performance   — per-strategy stats
  GET  /api/pnl-chart     — hourly bankroll data for chart
  WS   /ws/live           — push updates to browser
  GET  /                  — serves index.html
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger(__name__)

app = FastAPI(title="PyPolyDumb Dashboard", version="1.0.0")

# These are injected by main.py after bot initialization
_registry: dict = {}


def register(key: str, obj):
    """Called by main.py to inject live objects into the dashboard."""
    _registry[key] = obj


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    guard = _registry.get("guard")
    return JSONResponse({
        "mode": _registry.get("mode", "unknown"),
        "active_strategies": _registry.get("active_strategies", []),
        "circuit_breaker": guard.status() if guard else {},
        "btc_price": _get_btc_price(),
        "window_slug": _get_window_slug(),
        "seconds_remaining": _get_seconds_remaining(),
    })


@app.get("/api/bankroll")
async def get_bankroll():
    bm = _registry.get("bankroll")
    tracker = _registry.get("tracker")
    if bm is None:
        return JSONResponse({"error": "Not initialized"}, status_code=503)

    summary = bm.summary()
    tracker_summary = tracker.summary() if tracker else {}
    return JSONResponse({**summary, **tracker_summary})


@app.get("/api/trades")
async def get_trades():
    logger = _registry.get("logger")
    if logger is None:
        return JSONResponse([])
    trades = logger.recent_trades(50)
    return JSONResponse(trades)


@app.get("/api/performance")
async def get_performance():
    perf = _registry.get("performance")
    if perf is None:
        return JSONResponse({})
    return JSONResponse({
        "overall": perf.overall_stats(),
        "by_strategy": perf.all_strategy_stats(),
        "today": perf.daily_summary(),
    })


@app.get("/api/pnl-chart")
async def get_pnl_chart():
    logger = _registry.get("logger")
    if logger is None:
        return JSONResponse([])
    history = logger.bankroll_history(200)
    return JSONResponse(list(reversed(history)))


# ── WebSocket live feed ───────────────────────────────────────────────────────

_ws_connections: list[WebSocket] = []


@app.websocket("/ws/live")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_connections.append(ws)
    log.info("Dashboard WS connected (total: %d)", len(_ws_connections))
    try:
        while True:
            # Keep alive; server pushes updates
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_connections.remove(ws)
        log.info("Dashboard WS disconnected (total: %d)", len(_ws_connections))


async def broadcast(event_type: str, data: dict):
    """Broadcast event to all connected dashboard clients."""
    if not _ws_connections:
        return
    payload = json.dumps({"type": event_type, "data": data})
    dead = []
    for ws in _ws_connections:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_connections.remove(ws)


async def broadcast_trade(trade: dict):
    await broadcast("new_trade", trade)


async def broadcast_bankroll(bankroll: float, daily_pnl: float):
    await broadcast("bankroll_update", {"bankroll": bankroll, "daily_pnl": daily_pnl})


async def broadcast_price(btc_price: float, window_delta: float, seconds_remaining: float):
    await broadcast("price_update", {
        "btc_price": btc_price,
        "window_delta": window_delta,
        "seconds_remaining": seconds_remaining,
    })


# ── Static HTML ───────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>PyPolyDumb Dashboard</h1><p>index.html not found</p>")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_btc_price() -> float:
    feed = _registry.get("binance_feed")
    return feed.current_price if feed else 0.0


def _get_window_slug() -> str:
    watcher = _registry.get("watcher")
    if watcher and watcher.current:
        return watcher.current.slug
    return ""


def _get_seconds_remaining() -> float:
    watcher = _registry.get("watcher")
    if watcher and watcher.current:
        return watcher.current.seconds_remaining()
    return 0.0
