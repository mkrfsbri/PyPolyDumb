"""
SQLite trade logger.

Schema:
  trades  — one row per trade
  windows — one row per resolved market window
"""

import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

import config

log = logging.getLogger(__name__)

CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    market_slug     TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    size_usdc       REAL    NOT NULL,
    shares          REAL    NOT NULL,
    order_id        TEXT,
    outcome         TEXT    DEFAULT 'PENDING',
    exit_price      REAL,
    pnl             REAL,
    window_delta    REAL,
    confidence      REAL,
    fee_paid        REAL    DEFAULT 0.0,
    mode            TEXT    NOT NULL,
    resolved_at     TEXT
);
"""

CREATE_WINDOWS = """
CREATE TABLE IF NOT EXISTS windows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT    UNIQUE,
    open_price      REAL,
    close_price     REAL,
    direction       TEXT,
    resolved_at     TEXT
);
"""

CREATE_BANKROLL_LOG = """
CREATE TABLE IF NOT EXISTS bankroll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    bankroll        REAL    NOT NULL,
    daily_pnl       REAL,
    note            TEXT
);
"""


@dataclass
class TradeRecord:
    strategy: str
    market_slug: str
    direction: str
    entry_price: float
    size_usdc: float
    shares: float
    window_delta: float = 0.0
    confidence: float = 0.0
    order_id: str = ""
    outcome: str = "PENDING"
    exit_price: float = 0.0
    pnl: float = 0.0
    fee_paid: float = 0.0


class TradeLogger:
    def __init__(self, db_path: str = config.DB_PATH):
        self._db_path = db_path
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(CREATE_TRADES)
            conn.execute(CREATE_WINDOWS)
            conn.execute(CREATE_BANKROLL_LOG)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Write ────────────────────────────────────────────────────────────────

    def log_trade(self, record: TradeRecord) -> int:
        ts = _now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO trades
                   (timestamp, strategy, market_slug, direction, entry_price,
                    size_usdc, shares, order_id, outcome, exit_price, pnl,
                    window_delta, confidence, fee_paid, mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, record.strategy, record.market_slug, record.direction,
                 record.entry_price, record.size_usdc, record.shares,
                 record.order_id, record.outcome, record.exit_price, record.pnl,
                 record.window_delta, record.confidence, record.fee_paid,
                 config.BOT_MODE),
            )
            return cur.lastrowid

    def update_trade_outcome(
        self, trade_id: int, outcome: str, exit_price: float, pnl: float
    ):
        with self._conn() as conn:
            conn.execute(
                """UPDATE trades SET outcome=?, exit_price=?, pnl=?, resolved_at=?
                   WHERE id=?""",
                (outcome, exit_price, pnl, _now_iso(), trade_id),
            )

    def log_window(
        self, slug: str, open_price: float, close_price: float, direction: str
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO windows
                   (slug, open_price, close_price, direction, resolved_at)
                   VALUES (?,?,?,?,?)""",
                (slug, open_price, close_price, direction, _now_iso()),
            )

    def log_bankroll(self, bankroll: float, daily_pnl: float, note: str = ""):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO bankroll_log (timestamp, bankroll, daily_pnl, note)
                   VALUES (?,?,?,?)""",
                (_now_iso(), bankroll, daily_pnl, note),
            )

    # ── Read ─────────────────────────────────────────────────────────────────

    def recent_trades(self, n: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            return [dict(r) for r in rows]

    def trades_by_strategy(self, strategy: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE strategy=? ORDER BY id DESC",
                (strategy,),
            ).fetchall()
            return [dict(r) for r in rows]

    def pending_trades(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE outcome='PENDING'"
            ).fetchall()
            return [dict(r) for r in rows]

    def bankroll_history(self, n: int = 200) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM bankroll_log ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            return [dict(r) for r in rows]


def _now_iso() -> str:
    import datetime
    return datetime.datetime.utcnow().isoformat() + "Z"
