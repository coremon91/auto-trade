"""Incremental collection into SQLite.

`collect`  : REST, all current perpetuals, resumes from the last stored timestamp.
             Run it every few hours (cron) so OI / long-short history keeps growing
             past Binance's 30-day window.
`backfill` : archive (data.binance.vision), includes delisted symbols, for training
             on a long and survivorship-free history.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import pandas as pd

from .binance import (HOUR_MS, BinanceArchive, BinanceREST, day_range, month_range,
                      to_hourly_last)
from .config import Config
from .storage import Store


def _now_ms() -> int:
    return int(time.time() * 1000)


def collect(cfg: Config, days: int = 45, symbols: list[str] | None = None,
            log: Callable[[str], None] = print, rest: BinanceREST | None = None) -> dict:
    rest = rest or BinanceREST(cfg.fapi_base, cfg.request_sleep, log=log)
    store = Store(cfg.db_path)
    syms = rest.perpetual_symbols(cfg.quote_asset)
    store.upsert_symbols(syms)
    active = {s["symbol"] for s in syms if s["status"] == "TRADING"}
    gone = store.mark_missing_as_delisted({s["symbol"] for s in syms})
    if gone:
        log(f"no longer listed (kept in DB): {', '.join(gone)}")
    targets = symbols or sorted(active)
    default_start = _now_ms() - days * 24 * HOUR_MS
    stats = {"symbols": len(targets), "klines": 0, "oi": 0, "funding": 0, "ls": 0, "errors": 0}

    for i, sym in enumerate(targets, 1):
        try:
            last = store.last_ts("klines", sym, cfg.base_interval)
            start = (last + HOUR_MS) if last else default_start
            stats["klines"] += store.upsert_klines(sym, cfg.base_interval,
                                                   rest.klines(sym, cfg.base_interval, start))

            last = store.last_ts("open_interest", sym)
            stats["oi"] += store.upsert_oi(sym, rest.open_interest_hist(sym, "1h", (last or 0) + 1))

            last = store.last_ts("funding", sym)
            stats["funding"] += store.upsert_funding(sym, rest.funding(sym, (last + 1) if last else default_start))

            for kind in ("global_ratio", "top_pos_ratio", "taker_ratio"):
                last = store.last_ts_where_set(sym, kind)
                stats["ls"] += store.upsert_long_short(sym, rest.ratio_hist(sym, kind, "1h", (last or 0) + 1))
        except Exception as e:  # keep going; one bad symbol must not stop the run
            stats["errors"] += 1
            log(f"[{sym}] error: {e!r}")
        if i % 25 == 0 or i == len(targets):
            log(f"collect {i}/{len(targets)}")
    store.close()
    return stats


def backfill(cfg: Config, start: date, end: date | None = None, symbols: list[str] | None = None,
             with_metrics: bool = True, log: Callable[[str], None] = print,
             archive: BinanceArchive | None = None) -> dict:
    """Load monthly 1h klines + funding and daily OI/long-short metrics from the archive.
    Symbols default to every USDT symbol in the archive, delisted ones included."""
    archive = archive or BinanceArchive(cfg.archive_base, log=log)
    end = end or (datetime.now(timezone.utc).date() - timedelta(days=1))
    store = Store(cfg.db_path)
    syms = symbols or [s for s in archive.list_symbols() if s.endswith(cfg.quote_asset)]
    known = set(store.symbols())
    store.upsert_symbols([{"symbol": s, "status": None if s in known else "ARCHIVE"}
                          for s in syms if s not in known])
    stats = {"symbols": len(syms), "klines": 0, "oi": 0, "funding": 0, "ls": 0, "errors": 0}

    for i, sym in enumerate(syms, 1):
        try:
            for y, m in month_range(start, end):
                stats["klines"] += store.upsert_klines(
                    sym, cfg.base_interval, archive.monthly_klines(sym, cfg.base_interval, y, m))
                stats["funding"] += store.upsert_funding(sym, archive.monthly_funding(sym, y, m))
            if with_metrics:
                first_k = store.conn.execute(
                    "SELECT MIN(ts) FROM klines WHERE symbol=?", (sym,)).fetchone()[0]
                if first_k is None:
                    continue
                d0 = max(start, datetime.fromtimestamp(first_k / 1000, timezone.utc).date())
                frames = [f for d in day_range(d0, end) if (f := archive.daily_metrics(sym, d)) is not None]
                if frames:
                    met = pd.concat(frames)
                    stats["oi"] += store.upsert_oi(sym, to_hourly_last(met, ["oi", "oi_value"]).dropna())
                    stats["ls"] += store.upsert_long_short(
                        sym, to_hourly_last(met, ["global_ratio", "top_pos_ratio", "taker_ratio"]))
        except Exception as e:
            stats["errors"] += 1
            log(f"[{sym}] error: {e!r}")
        log(f"backfill {i}/{len(syms)} {sym}")
    store.close()
    return stats
