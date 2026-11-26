"""Public Binance USDT-M futures data (no API key).

Two sources:
  * REST (fapi.binance.com): live data. OI / long-short history only goes back ~30 days.
  * Archive (data.binance.vision): daily/monthly zip files, including delisted
    symbols. The daily "metrics" files carry 5-minute OI and long/short ratios for
    long periods, which fixes the 30-day OI limit and survivorship bias for backfills.
"""
from __future__ import annotations

import io
import re
import time
import zipfile
from datetime import date, timedelta
from typing import Callable

import pandas as pd
import requests

HOUR_MS = 3_600_000
INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": HOUR_MS,
               "2h": 2 * HOUR_MS, "4h": 4 * HOUR_MS, "1d": 24 * HOUR_MS}


class BinanceREST:
    def __init__(self, base: str = "https://fapi.binance.com", sleep: float = 0.12,
                 session: requests.Session | None = None, log: Callable[[str], None] = print):
        self.base = base.rstrip("/")
        self.sleep = sleep
        self.s = session or requests.Session()
        self.log = log
        self._last_data_call = 0.0

    # /futures/data/* (OI, long/short) is limited to ~1000 requests / 5 min per IP
    DATA_MIN_GAP = 0.32

    def _get(self, path: str, params: dict | None = None, retries: int = 5):
        url = self.base + path
        if path.startswith("/futures/data/"):
            gap = time.time() - self._last_data_call
            if gap < self.DATA_MIN_GAP:
                time.sleep(self.DATA_MIN_GAP - gap)
            self._last_data_call = time.time()
        for attempt in range(retries):
            try:
                r = self.s.get(url, params=params, timeout=20)
                if r.status_code in (418, 429):  # rate limited / banned: back off hard
                    wait = int(r.headers.get("Retry-After", 30 * (attempt + 1)))
                    self.log(f"rate limited ({r.status_code}), sleeping {wait}s")
                    time.sleep(wait)
                    continue
                if r.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                time.sleep(self.sleep)
                return r.json()
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise
                self.log(f"request error {e!r}, retrying")
                time.sleep(2 ** attempt)
        raise RuntimeError(f"failed GET {url} {params}")

    # ---------- symbols ----------
    def perpetual_symbols(self, quote: str = "USDT") -> list[dict]:
        info = self._get("/fapi/v1/exchangeInfo")
        out = []
        for s in info["symbols"]:
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != quote:
                continue
            out.append({"symbol": s["symbol"], "status": s.get("status"),
                        "onboard_ms": s.get("onboardDate")})
        return out

    # ---------- klines ----------
    def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
        end_ms = end_ms or int(time.time() * 1000)
        step = INTERVAL_MS[interval]
        rows, cur = [], start_ms
        while cur < end_ms:
            data = self._get("/fapi/v1/klines", {"symbol": symbol, "interval": interval,
                                                  "startTime": cur, "endTime": end_ms, "limit": 1500})
            if not data:
                break
            rows.extend(data)
            nxt = int(data[-1][0]) + step
            if nxt <= cur or len(data) < 1500:
                break
            cur = nxt
        return parse_klines(rows, drop_open_bar=True)

    # ---------- open interest (≈30 days max) ----------
    def open_interest_hist(self, symbol: str, period: str = "1h", start_ms: int | None = None) -> pd.DataFrame:
        now = int(time.time() * 1000)
        start = max(start_ms or 0, now - 29 * 24 * HOUR_MS)
        rows, cur = [], start
        while cur < now:
            data = self._get("/futures/data/openInterestHist",
                             {"symbol": symbol, "period": period, "limit": 500,
                              "startTime": cur, "endTime": now})
            if not data:
                break
            rows.extend(data)
            nxt = int(data[-1]["timestamp"]) + INTERVAL_MS[period]
            if nxt <= cur or len(data) < 500:
                break
            cur = nxt
        if not rows:
            return pd.DataFrame(columns=["ts", "oi", "oi_value"])
        df = pd.DataFrame(rows)
        return pd.DataFrame({"ts": df["timestamp"].astype("int64"),
                             "oi": df["sumOpenInterest"].astype(float),
                             "oi_value": df["sumOpenInterestValue"].astype(float)}).drop_duplicates("ts")

    # ---------- funding ----------
    def funding(self, symbol: str, start_ms: int = 0) -> pd.DataFrame:
        rows, cur, now = [], start_ms, int(time.time() * 1000)
        while cur < now:
            data = self._get("/fapi/v1/fundingRate",
                             {"symbol": symbol, "startTime": cur, "endTime": now, "limit": 1000})
            if not data:
                break
            rows.extend(data)
            nxt = int(data[-1]["fundingTime"]) + 1
            if nxt <= cur or len(data) < 1000:
                break
            cur = nxt
        if not rows:
            return pd.DataFrame(columns=["ts", "rate"])
        df = pd.DataFrame(rows)
        return pd.DataFrame({"ts": df["fundingTime"].astype("int64"),
                             "rate": df["fundingRate"].astype(float)}).drop_duplicates("ts")

    # ---------- long/short ratios (≈30 days max) ----------
    _LS = {
        "global_ratio": ("/futures/data/globalLongShortAccountRatio", "longShortRatio"),
        "top_pos_ratio": ("/futures/data/topLongShortPositionRatio", "longShortRatio"),
        "taker_ratio": ("/futures/data/takerlongshortRatio", "buySellRatio"),
    }

    def ratio_hist(self, symbol: str, kind: str, period: str = "1h", start_ms: int | None = None) -> pd.DataFrame:
        path, field = self._LS[kind]
        now = int(time.time() * 1000)
        start = max(start_ms or 0, now - 29 * 24 * HOUR_MS)
        rows, cur = [], start
        while cur < now:
            data = self._get(path, {"symbol": symbol, "period": period, "limit": 500,
                                    "startTime": cur, "endTime": now})
            if not data:
                break
            rows.extend(data)
            nxt = int(data[-1]["timestamp"]) + INTERVAL_MS[period]
            if nxt <= cur or len(data) < 500:
                break
            cur = nxt
        if not rows:
            return pd.DataFrame(columns=["ts", kind])
        df = pd.DataFrame(rows)
        return pd.DataFrame({"ts": df["timestamp"].astype("int64"),
                             kind: df[field].astype(float)}).drop_duplicates("ts")


def parse_klines(rows: list, drop_open_bar: bool = False) -> pd.DataFrame:
    cols = ["ts", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
            "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "quote_volume",
                                     "trades", "taker_buy_base", "taker_buy_quote"])
    df = pd.DataFrame(rows, columns=cols[: len(rows[0])])
    df = df.drop(columns=[c for c in ("close_time", "ignore") if c in df])
    df = df.apply(pd.to_numeric, errors="coerce")
    df["ts"] = df["ts"].astype("int64")
    if drop_open_bar:  # the last bar is still forming; don't store it
        df = df[df["ts"] + _bar_ms(df) <= int(time.time() * 1000)]
    return df.drop_duplicates("ts").reset_index(drop=True)


def _bar_ms(df: pd.DataFrame) -> int:
    if len(df) >= 2:
        return int(df["ts"].diff().median())
    return HOUR_MS


# ======================================================================
# data.binance.vision archive (history incl. delisted symbols)
# ======================================================================
class BinanceArchive:
    LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"

    def __init__(self, base: str = "https://data.binance.vision", session: requests.Session | None = None,
                 log: Callable[[str], None] = print):
        self.base = base.rstrip("/")
        self.s = session or requests.Session()
        self.log = log

    def list_symbols(self, prefix: str = "data/futures/um/monthly/klines/") -> list[str]:
        """All symbols that ever had archived futures klines (includes delisted ones)."""
        out, marker = [], ""
        while True:
            r = self.s.get(self.LIST_URL, params={"delimiter": "/", "prefix": prefix, "marker": marker},
                           timeout=30)
            r.raise_for_status()
            found = re.findall(r"<Prefix>([^<]+)</Prefix>", r.text)
            syms = [p[len(prefix):].strip("/") for p in found if p != prefix]
            out.extend(s for s in syms if s)
            if "<IsTruncated>true</IsTruncated>" not in r.text or not found:
                break
            marker = found[-1]
        return sorted(set(out))

    def _zip_csv(self, path: str) -> pd.DataFrame | None:
        r = self.s.get(f"{self.base}/{path}", timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            name = z.namelist()[0]
            raw = z.read(name)
        return _read_csv_maybe_header(raw)

    def monthly_klines(self, symbol: str, interval: str, year: int, month: int) -> pd.DataFrame:
        df = self._zip_csv(f"data/futures/um/monthly/klines/{symbol}/{interval}/"
                           f"{symbol}-{interval}-{year}-{month:02d}.zip")
        if df is None:
            return parse_klines([])
        return parse_klines(df.iloc[:, :12].values.tolist())

    def monthly_funding(self, symbol: str, year: int, month: int) -> pd.DataFrame:
        df = self._zip_csv(f"data/futures/um/monthly/fundingRate/{symbol}/"
                           f"{symbol}-fundingRate-{year}-{month:02d}.zip")
        if df is None or df.empty:
            return pd.DataFrame(columns=["ts", "rate"])
        # columns: calc_time, funding_interval_hours, last_funding_rate
        return pd.DataFrame({"ts": pd.to_numeric(df.iloc[:, 0]).astype("int64"),
                             "rate": pd.to_numeric(df.iloc[:, -1]).astype(float)})

    def daily_metrics(self, symbol: str, day: date) -> pd.DataFrame | None:
        """5-minute OI + long/short metrics for one day."""
        df = self._zip_csv(f"data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day.isoformat()}.zip")
        if df is None or df.empty:
            return None
        df.columns = [str(c).strip() for c in df.columns]
        ts = pd.to_datetime(df["create_time"], utc=True).astype("int64") // 1_000_000
        return pd.DataFrame({
            "ts": ts.astype("int64"),
            "oi": pd.to_numeric(df.get("sum_open_interest"), errors="coerce"),
            "oi_value": pd.to_numeric(df.get("sum_open_interest_value"), errors="coerce"),
            "global_ratio": pd.to_numeric(df.get("count_long_short_ratio"), errors="coerce"),
            "top_pos_ratio": pd.to_numeric(df.get("sum_toptrader_long_short_ratio"), errors="coerce"),
            "taker_ratio": pd.to_numeric(df.get("sum_taker_long_short_vol_ratio"), errors="coerce"),
        })


def _read_csv_maybe_header(raw: bytes) -> pd.DataFrame:
    first = raw.split(b"\n", 1)[0]
    has_header = bool(re.search(rb"[a-zA-Z_]{3,}", first.split(b",")[0]))
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)


def month_range(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def day_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def to_hourly_last(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Thin 5m snapshots to one per hour (the last one in each hour), keeping each
    snapshot's real timestamp so later as-of joins never look ahead."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["ts", *cols])
    d = df.sort_values("ts").copy()
    d["hour"] = d["ts"] // HOUR_MS
    return d.groupby("hour").tail(1)[["ts", *cols]].reset_index(drop=True)
