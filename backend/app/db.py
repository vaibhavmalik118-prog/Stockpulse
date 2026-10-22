"""SQLite persistence.

All queries run in a worker thread (asyncio.to_thread) so database I/O never
blocks the event loop that delivers live prices.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    ticker TEXT NOT NULL, start INTEGER NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, start)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, target REAL NOT NULL, direction TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL, triggered_at REAL, triggered_price REAL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
    price REAL NOT NULL, realized_pnl REAL, ts REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY, quantity INTEGER NOT NULL, avg_cost REAL NOT NULL,
    stop_loss REAL, take_profit REAL, trail_pct REAL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
    type TEXT NOT NULL, price REAL NOT NULL,
    stop_loss REAL, take_profit REAL, trail_pct REAL,
    status TEXT NOT NULL DEFAULT 'open', note TEXT,
    created_at REAL NOT NULL, closed_at REAL, fill_price REAL
);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL, trades_today INTEGER NOT NULL DEFAULT 0, trade_day TEXT,
    loss_streak INTEGER NOT NULL DEFAULT 0, last_trade_at REAL, halted_day TEXT
);
"""

# columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("trades", "reason", "TEXT NOT NULL DEFAULT 'manual'"),
    ("positions", "stop_loss", "REAL"),
    ("positions", "take_profit", "REAL"),
    ("positions", "trail_pct", "REAL"),
]


class Database:
    def __init__(self, path: str, starting_cash: float = 100_000.0):
        self.path = path
        self.starting_cash = starting_cash
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ---- plumbing -------------------------------------------------------
    def _connect(self) -> None:
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            columns = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        if self._conn.execute("SELECT 1 FROM account WHERE id = 1").fetchone() is None:
            self._conn.execute("INSERT INTO account (id, cash) VALUES (1, ?)", (self.starting_cash,))
        self._conn.commit()

    def _run(self, fn, *args):
        with self._lock:
            result = fn(self._conn, *args)
            self._conn.commit()
            return result

    async def _q(self, fn, *args):
        return await asyncio.to_thread(self._run, fn, *args)

    async def init(self) -> None:
        await asyncio.to_thread(self._connect)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    # ---- candles --------------------------------------------------------
    async def save_candles(self, candles: Iterable[dict]) -> None:
        rows = [(c["ticker"], int(c["start"]), c["open"], c["high"], c["low"], c["close"], c.get("volume", 0))
                for c in candles]
        if not rows:
            return
        await self._q(lambda con, r: con.executemany(
            "INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?)", r), rows)

    async def save_candle(self, candle: dict) -> None:
        await self.save_candles([candle])

    async def recent_candles(self, ticker: str, since: int, limit: int = 1000) -> list[dict]:
        def q(con, t, s, n):
            rows = con.execute(
                "SELECT * FROM candles WHERE ticker = ? AND start >= ? ORDER BY start DESC LIMIT ?",
                (t, s, n)).fetchall()
            return [dict(r) for r in reversed(rows)]
        return await self._q(q, ticker, since, limit)

    # ---- alerts ---------------------------------------------------------
    async def add_alert(self, ticker: str, target: float, direction: str) -> dict:
        def q(con):
            cur = con.execute("INSERT INTO alerts (ticker, target, direction, created_at) VALUES (?, ?, ?, ?)",
                              (ticker, target, direction, time.time()))
            return dict(con.execute("SELECT * FROM alerts WHERE id = ?", (cur.lastrowid,)).fetchone())
        return await self._q(q)

    async def list_alerts(self, status: Optional[str] = None) -> list[dict]:
        def q(con):
            if status:
                rows = con.execute("SELECT * FROM alerts WHERE status = ? ORDER BY id DESC", (status,))
            else:
                rows = con.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 100")
            return [dict(r) for r in rows.fetchall()]
        return await self._q(q)

    async def delete_alert(self, alert_id: int) -> bool:
        return await self._q(lambda con: con.execute("DELETE FROM alerts WHERE id = ?", (alert_id,)).rowcount > 0)

    async def mark_alert_triggered(self, alert_id: int, price: float, ts: float) -> None:
        await self._q(lambda con: con.execute(
            "UPDATE alerts SET status = 'triggered', triggered_at = ?, triggered_price = ? WHERE id = ?",
            (ts, price, alert_id)))

    # ---- account, positions, trades ------------------------------------
    async def get_account(self) -> dict:
        return await self._q(lambda con: dict(con.execute("SELECT * FROM account WHERE id = 1").fetchone()))

    async def get_positions(self) -> dict[str, dict]:
        return await self._q(lambda con: {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM positions")})

    async def list_trades(self, limit: int = 50) -> list[dict]:
        return await self._q(lambda con: [dict(r) for r in con.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()])

    async def realized_pnl_total(self) -> float:
        return await self._q(lambda con: con.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM trades").fetchone()[0])

    async def commit_trade(self, account: dict, position: Optional[dict], ticker: str, trade: dict) -> dict:
        """Write the trade, the new position and the new account state in one transaction."""
        def q(con):
            con.execute(
                "UPDATE account SET cash = ?, trades_today = ?, trade_day = ?, loss_streak = ?, "
                "last_trade_at = ?, halted_day = ? WHERE id = 1",
                (account["cash"], account["trades_today"], account["trade_day"], account["loss_streak"],
                 account["last_trade_at"], account["halted_day"]))
            if position is None or position["quantity"] == 0:
                con.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
            else:
                con.execute("INSERT OR REPLACE INTO positions (ticker, quantity, avg_cost, stop_loss, take_profit, "
                            "trail_pct) VALUES (?, ?, ?, ?, ?, ?)",
                            (ticker, position["quantity"], position["avg_cost"],
                             position.get("stop_loss"), position.get("take_profit"), position.get("trail_pct")))
            cur = con.execute(
                "INSERT INTO trades (ticker, side, quantity, price, realized_pnl, ts, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (trade["ticker"], trade["side"], trade["quantity"], trade["price"], trade["realized_pnl"], trade["ts"],
                 trade.get("reason", "manual")))
            return dict(con.execute("SELECT * FROM trades WHERE id = ?", (cur.lastrowid,)).fetchone())
        return await self._q(q)

    async def set_exits(self, ticker: str, stop_loss: Optional[float], take_profit: Optional[float],
                        trail_pct: Optional[float] = None) -> bool:
        return await self._q(lambda con: con.execute(
            "UPDATE positions SET stop_loss = ?, take_profit = ?, trail_pct = ? WHERE ticker = ?",
            (stop_loss, take_profit, trail_pct, ticker)).rowcount > 0)

    async def set_stop_loss(self, ticker: str, stop_loss: float) -> None:
        await self._q(lambda con: con.execute(
            "UPDATE positions SET stop_loss = ? WHERE ticker = ?", (stop_loss, ticker)))

    # ---- pending (limit / stop) orders ----------------------------------
    async def add_order(self, order: dict) -> dict:
        def q(con):
            cur = con.execute(
                "INSERT INTO orders (ticker, side, quantity, type, price, stop_loss, take_profit, trail_pct, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order["ticker"], order["side"], order["quantity"], order["type"], order["price"],
                 order.get("stop_loss"), order.get("take_profit"), order.get("trail_pct"), time.time()))
            return dict(con.execute("SELECT * FROM orders WHERE id = ?", (cur.lastrowid,)).fetchone())
        return await self._q(q)

    async def list_orders(self, status: Optional[str] = None, limit: int = 50) -> list[dict]:
        def q(con):
            if status:
                rows = con.execute("SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
            else:
                rows = con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows.fetchall()]
        return await self._q(q)

    async def close_order(self, order_id: int, status: str, note: Optional[str] = None,
                          fill_price: Optional[float] = None) -> Optional[dict]:
        """Move an open order to filled / cancelled / rejected. Returns None if it wasn't open."""
        def q(con):
            cur = con.execute("UPDATE orders SET status = ?, note = ?, fill_price = ?, closed_at = ? "
                              "WHERE id = ? AND status = 'open'", (status, note, fill_price, time.time(), order_id))
            if cur.rowcount == 0:
                return None
            return dict(con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
        return await self._q(q)

    async def save_account(self, account: dict) -> None:
        await self._q(lambda con: con.execute(
            "UPDATE account SET cash = ?, trades_today = ?, trade_day = ?, loss_streak = ?, "
            "last_trade_at = ?, halted_day = ? WHERE id = 1",
            (account["cash"], account["trades_today"], account["trade_day"], account["loss_streak"],
             account["last_trade_at"], account["halted_day"])))

    async def reset_account(self) -> None:
        def q(con):
            con.execute("DELETE FROM trades")
            con.execute("DELETE FROM positions")
            con.execute("DELETE FROM orders")
            con.execute("UPDATE account SET cash = ?, trades_today = 0, trade_day = NULL, loss_streak = 0, "
                        "last_trade_at = NULL, halted_day = NULL WHERE id = 1", (self.starting_cash,))
        await self._q(q)
