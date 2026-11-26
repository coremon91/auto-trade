"""SQLite storage. Data is upserted so collection can run repeatedly (cron) and
history accumulates beyond Binance's ~30-day limit for OI / long-short data.

Symbols are never deleted: once seen, a symbol stays in the DB even after it is
delisted, so training data is not survivorship-biased going forward.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Iterable

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    status TEXT,
    onboard_ms INTEGER,
    first_seen_ms INTEGER,
    last_seen_ms INTEGER,
    delisted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT, interval TEXT, ts INTEGER,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    quote_volume REAL, trades INTEGER, taker_buy_base REAL, taker_buy_quote REAL,
    PRIMARY KEY (symbol, interval, ts)
);
CREATE TABLE IF NOT EXISTS open_interest (
    symbol TEXT, ts INTEGER, oi REAL, oi_value REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS funding (
    symbol TEXT, ts INTEGER, rate REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS long_short (
    symbol TEXT, ts INTEGER,
    global_ratio REAL, top_pos_ratio REAL, taker_ratio REAL,
    PRIMARY KEY (symbol, ts)
);
"""

KLINE_COLS = ["open", "high", "low", "close", "volume", "quote_volume",
              "trades", "taker_buy_base", "taker_buy_quote"]


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    # ---------- symbols ----------
    def upsert_symbols(self, rows: Iterable[dict]):
        now = int(time.time() * 1000)
        for r in rows:
            self.conn.execute(
                """INSERT INTO symbols(symbol,status,onboard_ms,first_seen_ms,last_seen_ms,delisted)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     status=excluded.status,
                     onboard_ms=COALESCE(excluded.onboard_ms, symbols.onboard_ms),
                     last_seen_ms=excluded.last_seen_ms,
                     delisted=excluded.delisted""",
                (r["symbol"], r.get("status"), r.get("onboard_ms"), now, now,
                 int(r.get("status") not in (None, "TRADING"))),
            )
        self.conn.commit()

    def mark_missing_as_delisted(self, active: set[str]):
        cur = self.conn.execute("SELECT symbol FROM symbols WHERE delisted=0")
        gone = [s for (s,) in cur.fetchall() if s not in active]
        for s in gone:
            self.conn.execute("UPDATE symbols SET delisted=1, status='GONE' WHERE symbol=?", (s,))
        self.conn.commit()
        return gone

    def symbols(self, include_delisted: bool = True) -> list[str]:
        q = "SELECT symbol FROM symbols" + ("" if include_delisted else " WHERE delisted=0")
        return [s for (s,) in self.conn.execute(q + " ORDER BY symbol")]

    # ---------- generic upserts ----------
    def _upsert(self, table: str, cols: list[str], rows: list[tuple]):
        if not rows:
            return 0
        ph = ",".join("?" * len(cols))
        self.conn.executemany(
            f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({ph})", rows)
        self.conn.commit()
        return len(rows)

    def upsert_klines(self, symbol: str, interval: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = [(symbol, interval, int(r.ts), *[float(getattr(r, c)) for c in KLINE_COLS])
                for r in df.itertuples(index=False)]
        return self._upsert("klines", ["symbol", "interval", "ts", *KLINE_COLS], rows)

    def upsert_oi(self, symbol: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = [(symbol, int(r.ts), float(r.oi), float(r.oi_value)) for r in df.itertuples(index=False)]
        return self._upsert("open_interest", ["symbol", "ts", "oi", "oi_value"], rows)

    def upsert_funding(self, symbol: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = [(symbol, int(r.ts), float(r.rate)) for r in df.itertuples(index=False)]
        return self._upsert("funding", ["symbol", "ts", "rate"], rows)

    def upsert_long_short(self, symbol: str, df: pd.DataFrame) -> int:
        """df columns: ts + any of global_ratio, top_pos_ratio, taker_ratio.
        Merges with existing rows so separate endpoints can fill separate columns."""
        if df is None or df.empty:
            return 0
        cols = ["global_ratio", "top_pos_ratio", "taker_ratio"]
        old = pd.read_sql_query(
            "SELECT * FROM long_short WHERE symbol=? AND ts BETWEEN ? AND ?", self.conn,
            params=(symbol, int(df.ts.min()), int(df.ts.max()))).drop(columns="symbol")
        merged = df.copy()
        for c in cols:
            if c not in merged:
                merged[c] = float("nan")
        if not old.empty:
            merged = merged.set_index("ts")[cols].combine_first(old.set_index("ts")[cols]).reset_index()
        rows = [(symbol, int(r.ts), *[None if pd.isna(getattr(r, c)) else float(getattr(r, c)) for c in cols])
                for r in merged.itertuples(index=False)]
        return self._upsert("long_short", ["symbol", "ts", *cols], rows)

    # ---------- reads ----------
    def last_ts(self, table: str, symbol: str, interval: str | None = None):
        q = f"SELECT MAX(ts) FROM {table} WHERE symbol=?"
        params: list = [symbol]
        if interval:
            q += " AND interval=?"
            params.append(interval)
        (v,) = self.conn.execute(q, params).fetchone()
        return v

    def last_ts_where_set(self, symbol: str, col: str):
        (v,) = self.conn.execute(
            f"SELECT MAX(ts) FROM long_short WHERE symbol=? AND {col} IS NOT NULL", (symbol,)).fetchone()
        return v

    def read(self, table: str, symbol: str | None = None, interval: str | None = None,
             since_ms: int | None = None) -> pd.DataFrame:
        q, params = f"SELECT * FROM {table} WHERE 1=1", []
        if symbol:
            q += " AND symbol=?"; params.append(symbol)
        if interval:
            q += " AND interval=?"; params.append(interval)
        if since_ms is not None:
            q += " AND ts>=?"; params.append(since_ms)
        return pd.read_sql_query(q + " ORDER BY symbol, ts", self.conn, params=params)

    def symbol_meta(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM symbols", self.conn)
