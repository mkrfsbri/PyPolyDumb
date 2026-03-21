"""Central configuration — loads .env and exposes typed constants."""

import os
import math
from dotenv import load_dotenv

load_dotenv()

# ── Polymarket credentials ────────────────────────────────────────────────────
POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET: str = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")
POLY_FUNDER_ADDRESS: str = os.getenv("POLY_FUNDER_ADDRESS", "")

# ── Polymarket endpoints ──────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ── Binance endpoints ─────────────────────────────────────────────────────────
BINANCE_WS = "wss://stream.binance.com:9443/stream"
BINANCE_STREAMS = "btcusdt@trade/btcusdt@kline_1m"

# ── Market windows ────────────────────────────────────────────────────────────
WINDOW_SECONDS = {"5m": 300, "15m": 900}
MARKET_TYPE: str = os.getenv("MARKET_TYPE", "5m")
WINDOW_INTERVAL: int = WINDOW_SECONDS.get(MARKET_TYPE, 300)

# ── Bot mode ──────────────────────────────────────────────────────────────────
BOT_MODE: str = os.getenv("BOT_MODE", "dry_run")   # dry_run | paper | live
CONFIRM_LIVE: str = os.getenv("CONFIRM_LIVE", "no")
ACTIVE_STRATEGIES: list[str] = [
    s.strip() for s in os.getenv("ACTIVE_STRATEGIES", "endcycle_sniper").split(",") if s.strip()
]

# ── Bankroll & risk ───────────────────────────────────────────────────────────
STARTING_BANKROLL: float = float(os.getenv("STARTING_BANKROLL", "100.0"))
MAX_BET_FRACTION: float = float(os.getenv("MAX_BET_FRACTION", "0.10"))
MAX_DAILY_LOSS_FRACTION: float = float(os.getenv("MAX_DAILY_LOSS_FRACTION", "0.20"))
MIN_BANKROLL: float = float(os.getenv("MIN_BANKROLL", "20.0"))
MAX_CONCURRENT_POSITIONS: int = 3
CIRCUIT_BREAK_LOSSES: int = 3       # consecutive losses before pause
CIRCUIT_BREAK_DAILY_LOSS: float = 0.15  # daily loss fraction before pause
COOLDOWN_SECONDS: int = 900          # 15 min cooldown after circuit break

# ── Strategy-specific constants ───────────────────────────────────────────────
ENDCYCLE_HIGH_SCORE: float = 14.0   # out of 20
ENDCYCLE_MED_SCORE: float = 7.0     # lowered from 10 — real markets rarely exceed 10 on small delta
ENDCYCLE_ACTIVATION_SECS: int = 30  # activate T-30s before window end
ENDCYCLE_DEACTIVATE_SECS: int = 10  # stop at T-10s

PAIR_COST_MAX: float = 0.97         # max pair cost to enter
PAIR_COST_TRIGGER: float = 0.35     # buy when either side < this

LATENCY_MIN_EDGE: float = 0.08      # minimum spot-vs-market edge
LATENCY_CANCEL_SECS: int = 15       # cancel unfilled order after 15s

FLASH_CRASH_THRESHOLD: float = 0.25  # 25% drop triggers
FLASH_CRASH_HEDGE_TARGET: float = 0.95
FLASH_CRASH_MAX_WAIT: int = 60

MC_PATHS: int = 10_000
MC_MIN_EDGE: float = 0.08

MM_BASE_SPREAD: float = 0.03
MM_REFRESH_SECS: float = 5.0
MM_MAX_INVENTORY: float = 20.0

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8000"))

# ── Persistence ───────────────────────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "trades.db")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ── Minimum order ────────────────────────────────────────────────────────────
POLY_MIN_SHARES: int = 5            # Polymarket minimum 5 shares per order


# ── Pure functions ────────────────────────────────────────────────────────────

def calc_taker_fee(price: float, fee_constant: float = 1.0) -> float:
    """2026 taker fee formula. Prefer maker orders (fee = 0)."""
    p = price
    return fee_constant * 0.25 * (p * (1.0 - p)) ** 2


def estimate_token_price(delta_pct: float) -> float:
    """Estimate market token price from window delta. Used for backtesting."""
    d = abs(delta_pct)
    if d < 0.005:
        return 0.50
    elif d < 0.02:
        return 0.55
    elif d < 0.05:
        return 0.65
    elif d < 0.10:
        return 0.80
    elif d < 0.15:
        return 0.92
    else:
        return 0.97


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def is_live_mode() -> bool:
    return BOT_MODE == "live" and CONFIRM_LIVE.lower() == "yes"


def validate_credentials() -> bool:
    """Return True if all credentials are configured."""
    return all([POLY_PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET,
                POLY_API_PASSPHRASE, POLY_FUNDER_ADDRESS])
