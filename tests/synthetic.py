"""Synthetic market used by the tests: random walks with injected pumps that are
preceded by an OI build-up while price is flat (so a model has something to learn)."""
import numpy as np
import pandas as pd

from screener.binance import HOUR_MS
from screener.storage import Store

T0 = 1_700_000_000_000 // HOUR_MS * HOUR_MS


def make_symbol(rng, n_hours, pump_every=None, start=T0):
    ts = start + np.arange(n_hours) * HOUR_MS
    r = rng.normal(0, 0.008, n_hours)
    oi_r = rng.normal(0, 0.004, n_hours)
    pumps = []
    if pump_every:
        for p0 in range(400, n_hours - 100, pump_every):
            p0 += int(rng.integers(-40, 40))
            oi_r[p0 - 24:p0] += 0.006          # OI +~15% over 24h ...
            r[p0 - 24:p0] *= 0.3                # ... while price is quiet
            r[p0:p0 + 12] += 0.025              # then ~+30% in 12h
            pumps.append(p0)
    close = 10 * np.exp(np.cumsum(r))
    high = close * (1 + np.abs(rng.normal(0, 0.004, n_hours)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n_hours)))
    open_ = np.r_[close[0], close[:-1]]
    vol = rng.lognormal(10, 0.3, n_hours)
    kl = pd.DataFrame({"ts": ts, "open": open_, "high": np.maximum(high, open_), "low": np.minimum(low, open_),
                       "close": close, "volume": vol, "quote_volume": vol * close,
                       "trades": (vol / 10).astype(int), "taker_buy_base": vol * 0.5,
                       "taker_buy_quote": vol * close * 0.5})
    oi = 1e6 * np.exp(np.cumsum(oi_r))
    oi_df = pd.DataFrame({"ts": ts + HOUR_MS, "oi": oi, "oi_value": oi * close})
    fr = pd.DataFrame({"ts": ts[::8] + HOUR_MS, "rate": rng.normal(0.0001, 0.0002, len(ts[::8]))})
    ls = pd.DataFrame({"ts": ts + HOUR_MS, "global_ratio": rng.normal(1.5, 0.2, n_hours),
                       "top_pos_ratio": rng.normal(1.2, 0.2, n_hours),
                       "taker_ratio": rng.normal(1.0, 0.1, n_hours)})
    return kl, oi_df, fr, ls, pumps


def build_store(path, n_symbols=24, n_hours=24 * 75, seed=0, delisted=("DEADUSDT",)):
    rng = np.random.default_rng(seed)
    st = Store(path)
    names = ["BTCUSDT"] + [f"S{i:02d}USDT" for i in range(n_symbols - 1 - len(delisted))] + list(delisted)
    st.upsert_symbols([{"symbol": s, "status": "SETTLING" if s in delisted else "TRADING",
                        "onboard_ms": T0} for s in names])
    for s in names:
        n = n_hours // 2 if s in delisted else n_hours   # delisted symbol stops mid-way
        kl, oi, fr, ls, _ = make_symbol(rng, n, pump_every=None if s == "BTCUSDT" else 300)
        st.upsert_klines(s, "1h", kl)
        st.upsert_oi(s, oi)
        st.upsert_funding(s, fr)
        st.upsert_long_short(s, ls)
    return st, names
