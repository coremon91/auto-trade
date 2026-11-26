"""Feature + label construction on a 1h grid.

Timing convention (no look-ahead):
  * a kline row with open time `ts` is only known at its close, `t = ts + 1h`.
  * every other series (OI, long/short, funding) is joined as-of `t`
    (latest value with timestamp <= t).
  * labels look strictly forward: the next `horizon_h` bars after t.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .binance import HOUR_MS
from .config import Config

FEATURES = [
    # price / volatility
    "ret_1h", "ret_4h", "ret_8h", "ret_24h", "ret_72h", "ret_7d", "ret_30d",
    "dd_from_30d_high", "up_from_30d_low", "range_pos_30d",
    "bbw_80h", "bbw_pct_30d", "atr_24h",
    # volume
    "vol_8h_ratio", "vol_24h_ratio", "log_qv_24h", "taker_buy_share_24h", "trades_24h_ratio",
    # open interest
    "oi_chg_4h", "oi_chg_24h", "oi_chg_72h", "oi_to_vol",
    # funding / positioning
    "funding_last", "funding_min_72h", "funding_mean_72h",
    "global_ls", "global_ls_chg_24h", "top_pos_ls", "taker_ls_24h",
    # history / market
    "past_pumps_60d", "max_runup_30d", "age_days",
    "btc_ret_24h", "btc_ret_72h", "rel_ret_24h_btc",
    # cross-sectional ranks (0..1 among all symbols at the same hour)
    "xs_ret_7d", "xs_bbw", "xs_vol_8h", "xs_oi_chg_24h", "xs_funding",
    # rule flags from the precursor study
    "f_oi_build_quiet", "f_mantra", "f_neg_funding", "f_quiet_vol_spike",
    "f_crash_oi", "f_hot_vol",
]

LABELS = ["fwd_max", "fwd_min", "y_pump", "y_big_pump", "y_dump", "y_pump_first"]


def _asof(grid_t: pd.Series, df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame({c: np.nan for c in cols}, index=grid_t.index)
    right = df[["ts", *cols]].dropna(subset=cols, how="all").sort_values("ts")
    right = right.rename(columns={"ts": "t"}).astype({"t": "int64"})
    left = pd.DataFrame({"t": grid_t.astype("int64").values})
    out = pd.merge_asof(left, right, on="t", direction="backward")
    out.index = grid_t.index
    return out[cols]


def _pct(s: pd.Series, n: int) -> pd.Series:
    return s / s.shift(n) - 1.0


def symbol_frame(kl: pd.DataFrame, oi: pd.DataFrame | None, fr: pd.DataFrame | None,
                 ls: pd.DataFrame | None, cfg: Config, onboard_ms: int | None = None) -> pd.DataFrame:
    """Per-symbol features + labels on a gap-free 1h grid."""
    if kl is None or len(kl) < 48:
        return pd.DataFrame()
    kl = kl.sort_values("ts").drop_duplicates("ts")
    grid = np.arange(kl.ts.iloc[0], kl.ts.iloc[-1] + HOUR_MS, HOUR_MS, dtype="int64")
    d = kl.set_index("ts").reindex(grid)
    d.index.name = "ts"
    d = d.reset_index()
    d["t"] = d["ts"] + HOUR_MS

    c, h, l = d["close"], d["high"], d["low"]
    qv = d["quote_volume"].fillna(0.0)
    cf = c.ffill()

    f = pd.DataFrame(index=d.index)
    f["ts"], f["t"], f["close"] = d["ts"], d["t"], c
    for name, n in [("ret_1h", 1), ("ret_4h", 4), ("ret_8h", 8), ("ret_24h", 24),
                    ("ret_72h", 72), ("ret_7d", 168), ("ret_30d", 720)]:
        f[name] = _pct(cf, n)

    hi30 = h.rolling(720, min_periods=72).max()
    lo30 = l.rolling(720, min_periods=72).min()
    f["dd_from_30d_high"] = cf / hi30 - 1
    f["up_from_30d_low"] = cf / lo30 - 1
    f["range_pos_30d"] = (cf - lo30) / (hi30 - lo30).replace(0, np.nan)

    # BB width on a 4h-equivalent 20-bar window (80 hourly bars)
    ma = cf.rolling(80, min_periods=40).mean()
    sd = cf.rolling(80, min_periods=40).std()
    f["bbw_80h"] = 4 * sd / ma
    f["bbw_pct_30d"] = f["bbw_80h"].rolling(720, min_periods=168).rank(pct=True)
    f["atr_24h"] = ((h - l) / cf).rolling(24, min_periods=12).mean()

    v8 = qv.rolling(8, min_periods=4).sum()
    v24 = qv.rolling(24, min_periods=12).sum()
    base8 = v8.shift(8).rolling(168, min_periods=48).mean()   # "normal" 8h volume over prior week
    base24 = v24.shift(24).rolling(168, min_periods=48).mean()
    f["vol_8h_ratio"] = v8 / base8.replace(0, np.nan)
    f["vol_24h_ratio"] = v24 / base24.replace(0, np.nan)
    f["log_qv_24h"] = np.log10(v24 + 1)
    f["taker_buy_share_24h"] = d["taker_buy_quote"].fillna(0).rolling(24, min_periods=12).sum() / v24.replace(0, np.nan)
    tr24 = d["trades"].fillna(0).rolling(24, min_periods=12).sum()
    f["trades_24h_ratio"] = tr24 / tr24.shift(24).rolling(168, min_periods=48).mean().replace(0, np.nan)

    o = _asof(d["t"], oi, ["oi", "oi_value"])
    f["oi_chg_4h"] = _pct(o["oi"], 4)
    f["oi_chg_24h"] = _pct(o["oi"], 24)
    f["oi_chg_72h"] = _pct(o["oi"], 72)
    f["oi_to_vol"] = o["oi_value"] / v24.replace(0, np.nan)

    fu = _asof(d["t"], fr, ["rate"])["rate"]
    f["funding_last"] = fu
    f["funding_min_72h"] = fu.rolling(72, min_periods=8).min()
    f["funding_mean_72h"] = fu.rolling(72, min_periods=8).mean()

    r = _asof(d["t"], ls, ["global_ratio", "top_pos_ratio", "taker_ratio"])
    f["global_ls"] = r["global_ratio"]
    f["global_ls_chg_24h"] = _pct(r["global_ratio"], 24)
    f["top_pos_ls"] = r["top_pos_ratio"]
    f["taker_ls_24h"] = r["taker_ratio"].rolling(24, min_periods=6).mean()

    # repeat pumper: completed past +pump_pct run-ups (backward-looking only)
    runup = cf / l.rolling(cfg.horizon_h, min_periods=12).min() - 1
    onset = (runup >= cfg.pump_pct) & ~(runup.shift(1) >= cfg.pump_pct)
    f["past_pumps_60d"] = onset.astype(float).rolling(1440, min_periods=1).sum()
    f["max_runup_30d"] = runup.rolling(720, min_periods=24).max()
    start_ms = onboard_ms if onboard_ms else int(d["ts"].iloc[0])
    f["age_days"] = (d["t"] - start_ms) / (24 * HOUR_MS)

    # rule flags (study thresholds)
    f["f_oi_build_quiet"] = ((f.oi_chg_24h >= cfg.oi_build_pct) & (f.ret_24h.abs() < 0.05)).astype(float)
    f["f_mantra"] = ((f.oi_chg_24h >= cfg.oi_build_pct) & f.ret_24h.between(-0.12, 0.03)
                     & (f.ret_7d <= -0.15)).astype(float)
    f["f_neg_funding"] = (f.funding_last <= cfg.neg_funding).astype(float)
    f["f_quiet_vol_spike"] = ((f.vol_8h_ratio >= cfg.quiet_vol_mult)
                              & (f.ret_8h.abs() < cfg.quiet_vol_price)).astype(float)
    f["f_crash_oi"] = ((f.ret_24h <= -0.10) & (f.oi_chg_24h >= cfg.oi_build_pct)).astype(float)

    f = f.join(make_labels(h, l, c, cfg))
    return f[c.notna()].reset_index(drop=True)  # drop grid rows for missing bars


def make_labels(high: pd.Series, low: pd.Series, close: pd.Series, cfg: Config) -> pd.DataFrame:
    """Forward labels over bars t+1..t+H. Rows without a full future window get NaN
    (they are excluded from training, kept for scoring)."""
    H = cfg.horizon_h
    n = len(close)
    hv, lv, cv = high.to_numpy(float), low.to_numpy(float), close.to_numpy(float)
    out = pd.DataFrame(index=close.index, columns=LABELS, dtype=float)
    if n <= H:
        return out
    from numpy.lib.stride_tricks import sliding_window_view as swv
    fh = swv(hv[1:], H)        # row i -> highs of bars i+1..i+H
    fl = swv(lv[1:], H)
    m = n - H                  # rows that have a full future window
    fh, fl, c0 = fh[:m], fl[:m], cv[:m]
    complete = ~np.isnan(fh).any(1) & ~np.isnan(fl).any(1) & ~np.isnan(c0)
    with np.errstate(invalid="ignore", divide="ignore"):
        up = fh / c0[:, None] - 1
        dn = fl / c0[:, None] - 1
        fmax, fmin = up.max(1), dn.min(1)
        hit_up = up >= cfg.pump_pct
        hit_dn = dn <= -cfg.dump_pct
    first_up = np.where(hit_up.any(1), hit_up.argmax(1), H + 1)
    first_dn = np.where(hit_dn.any(1), hit_dn.argmax(1), H + 1)
    res = np.full((m, len(LABELS)), np.nan)
    res[:, 0], res[:, 1] = fmax, fmin
    res[:, 2] = fmax >= cfg.pump_pct
    res[:, 3] = fmax >= cfg.big_pump_pct
    res[:, 4] = fmin <= -cfg.dump_pct
    # pump reached strictly before the dump (same bar counts as dump: conservative)
    res[:, 5] = (first_up < first_dn) & (first_up <= H)
    res[~complete] = np.nan
    out.iloc[:m] = res
    return out


def add_cross_section(panel: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Market context (BTC) and same-hour cross-sectional ranks."""
    p = panel
    btc = p.loc[p.symbol == "BTCUSDT", ["t", "ret_24h", "ret_72h"]]
    if btc.empty:
        p["btc_ret_24h"] = np.nan
        p["btc_ret_72h"] = np.nan
    else:
        btc = btc.rename(columns={"ret_24h": "btc_ret_24h", "ret_72h": "btc_ret_72h"})
        p = p.drop(columns=[c for c in ("btc_ret_24h", "btc_ret_72h") if c in p]).merge(btc, on="t", how="left")
    p["rel_ret_24h_btc"] = p["ret_24h"] - p["btc_ret_24h"]
    g = p.groupby("t")
    for src, dst in [("ret_7d", "xs_ret_7d"), ("bbw_80h", "xs_bbw"), ("vol_8h_ratio", "xs_vol_8h"),
                     ("oi_chg_24h", "xs_oi_chg_24h"), ("funding_last", "xs_funding")]:
        p[dst] = g[src].rank(pct=True)
    p["f_hot_vol"] = ((p.xs_bbw >= 0.8) & (p.xs_ret_7d >= 0.8)).astype(float)
    return p


def build_panel(store, cfg: Config, symbols: list[str] | None = None, since_ms: int | None = None,
                log=print) -> pd.DataFrame:
    meta = store.symbol_meta().set_index("symbol")
    syms = symbols or store.symbols(include_delisted=True)
    frames = []
    pad = None if since_ms is None else since_ms - 62 * 24 * HOUR_MS  # warm-up for 60d features
    for i, s in enumerate(syms, 1):
        kl = store.read("klines", s, cfg.base_interval, pad)
        if kl.empty:
            continue
        fr = symbol_frame(kl, store.read("open_interest", s, since_ms=pad),
                          store.read("funding", s, since_ms=pad),
                          store.read("long_short", s, since_ms=pad), cfg,
                          onboard_ms=_int_or_none(meta["onboard_ms"].get(s)) if s in meta.index else None)
        if fr.empty:
            continue
        fr.insert(0, "symbol", s)
        if since_ms is not None:
            fr = fr[fr.t >= since_ms]
        frames.append(fr)
        if i % 100 == 0:
            log(f"features {i}/{len(syms)}")
    if not frames:
        return pd.DataFrame(columns=["symbol", "ts", "t", *FEATURES, *LABELS])
    panel = pd.concat(frames, ignore_index=True)
    panel = add_cross_section(panel, cfg)
    return panel


def _int_or_none(v):
    try:
        return None if v is None or pd.isna(v) else int(v)
    except (TypeError, ValueError):
        return None
