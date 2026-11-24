"""SQLite journal: every signal, the decision on it, every order and its result."""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry REAL, sl REAL, tp REAL,
    reason TEXT,
    setup_key TEXT NOT NULL,
    decision TEXT,
    note TEXT,
    UNIQUE (mode, symbol, setup_key)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER REFERENCES signals(id),
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume REAL, price REAL, sl REAL, tp REAL,
    ticket INTEGER,
    status TEXT NOT NULL,          -- open | closed | dry-run
    opened_at TEXT NOT NULL,
    closed_at TEXT, exit_price REAL, profit REAL
);
CREATE TABLE IF NOT EXISTS days (
    day TEXT NOT NULL,
    mode TEXT NOT NULL,
    start_equity REAL NOT NULL,
    PRIMARY KEY (day, mode)
);
"""


class Journal:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def record_signal(self, mode, symbol, sig, ts: datetime) -> int | None:
        """Returns the new id, or None if this setup was already journaled."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO signals (ts, mode, symbol, side, entry, sl, tp, reason, setup_key)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), mode, symbol, sig.side, sig.entry, sig.sl, sig.tp, sig.reason,
             sig.setup_key))
        self.db.commit()
        return cur.lastrowid if cur.rowcount else None

    def set_decision(self, signal_id: int, decision: str, note: str = ""):
        self.db.execute("UPDATE signals SET decision=?, note=? WHERE id=?",
                        (decision, note, signal_id))
        self.db.commit()

    def record_order(self, signal_id, mode, symbol, side, volume, price, sl, tp, ticket,
                     opened_at: datetime) -> int:
        status = "dry-run" if mode == "dry-run" else "open"
        cur = self.db.execute(
            "INSERT INTO orders (signal_id, mode, symbol, side, volume, price, sl, tp, ticket,"
            " status, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (signal_id, mode, symbol, side, volume, price, sl, tp, ticket, status,
             opened_at.isoformat()))
        self.db.commit()
        return cur.lastrowid

    def open_orders(self, mode: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM orders WHERE mode=? AND status='open' AND ticket IS NOT NULL",
            (mode,)).fetchall()

    def close_order(self, order_id: int, closed_at: datetime, exit_price: float, profit: float):
        self.db.execute(
            "UPDATE orders SET status='closed', closed_at=?, exit_price=?, profit=? WHERE id=?",
            (closed_at.isoformat(), exit_price, profit, order_id))
        self.db.commit()

    def orders_since(self, mode: str, since: datetime, symbol: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM orders WHERE mode=? AND opened_at>=?"
        args = [mode, since.isoformat()]
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        return self.db.execute(q, args).fetchone()[0]

    def day_start_equity(self, day: str, mode: str, equity_now: float) -> float:
        """First equity seen this SAST day; stored so a restart can't reset the loss cap."""
        self.db.execute("INSERT OR IGNORE INTO days (day, mode, start_equity) VALUES (?,?,?)",
                        (day, mode, equity_now))
        self.db.commit()
        return self.db.execute("SELECT start_equity FROM days WHERE day=? AND mode=?",
                               (day, mode)).fetchone()[0]

    def export_csv(self, out_dir: str | Path) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for table in ("signals", "orders"):
            cur = self.db.execute(f"SELECT * FROM {table} ORDER BY id")
            path = out_dir / f"{table}.csv"
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([c[0] for c in cur.description])
                w.writerows(cur.fetchall())
            written.append(path)
        return written
