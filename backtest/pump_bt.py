"""1-year backtest of the "bleed + precursor -> breakout long" alt strategy on Binance USDT-M perps.

Data comes only from data.binance.vision (public archive, includes delisted symbols), so it
runs anywhere with that host reachable (e.g. GitHub Actions).

  python backtest/pump_bt.py fetch --start 2025-11-01 --end 2026-11-26 --data data/bt
  python backtest/pump_bt.py run   --data data/bt --from 2025-11-27 --out results

Rules (4h bars):
  watch base ("bleed"):  7d return <= -15% and close within 8% of 14d low
  precursors (any):      24h avg funding <= -0.03% | 8h volume >= 3x 7d avg 8h volume with |8h ret| <= 3%
                         | 20-bar BB width in bottom 10% of last 30d
  entry (within 24h of a watch bar): 4h close > highest high of prior 6 bars and volume >= 1.5x 20-bar avg
  exit: SL = lowest low of last 6 bars, distance clamped to 3..8%; TP = 3R; max hold 72h (18 bars);
        if SL and TP fall in the same bar, SL is assumed. Costs 0.2% round trip.
Variants: A (base + any precursor), each precursor alone, bleed-only (base), plain breakout (entry rule
alone), OI (OI24 >= +10% and px24 in [-12%, +3%], optionally with 7d <= -15%) on the last 30 days.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

BASE = "https://data.binance.vision"
S3_LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BAR_MS = 4 * 3_600_000

# ---------------------------------------------------------------- fetch

_sess = requests.Session()


def _get(url: str, retries: int = 4) -> bytes | None:
    for a in range(retries):
        try:
            r = _sess.get(url, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.content
        except requests.RequestException:
            pass
        time.sleep(1.5 * (a + 1))
    return None


def list_symbols() -> list[str]:
    out, marker = [], ""
    for host in (S3_LIST, BASE):
        try:
            while True:
                url = f"{host}?delimiter=/&prefix=data/futures/um/monthly/klines/&marker={marker}"
                r = _sess.get(url, timeout=30)
                r.raise_for_status()
                txt = r.text
                prefs = re.findall(r"<Prefix>data/futures/um/monthly/klines/([^/<]+)/</Prefix>", txt)
                out.extend(prefs)
                if "<IsTruncated>true</IsTruncated>" not in txt or not prefs:
                    break
                marker = f"data/futures/um/monthly/klines/{prefs[-1]}/"
            if out:
                break
        except requests.RequestException as e:
            print("listing failed on", host, e)
    syms = sorted({s for s in out if s.endswith("USDT") and "_" not in s})
    return syms


def _read_csv_zip(b: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        raw = z.read(z.namelist()[0]).decode()
    first = raw.split("\n", 1)[0]
    has_header = not first.split(",")[0].strip().lstrip("-").isdigit()
    return pd.read_csv(io.StringIO(raw), header=0 if has_header else None)


def _norm_ms(s: pd.Series) -> pd.Series:
    s = s.astype("int64")
    return np.where(s > 10**14, s // 1000, s)


def months(start: date, end: date):
    d = date(start.year, start.month, 1)
    while d <= end:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def fetch_symbol(sym: str, start: date, end: date, oi_start: date, root: str) -> str:
    path = os.path.join(root, f"{sym}.npz")
    if os.path.exists(path):
        return "cached"
    last_full = date(end.year, end.month, 1) - timedelta(days=1)
    kl = []
    for m in months(start, last_full):
        b = _get(f"{BASE}/data/futures/um/monthly/klines/{sym}/4h/{sym}-4h-{m:%Y-%m}.zip")
        if b:
            kl.append(_read_csv_zip(b))
    d = date(end.year, end.month, 1)
    while d <= end:
        b = _get(f"{BASE}/data/futures/um/daily/klines/{sym}/4h/{sym}-4h-{d:%Y-%m-%d}.zip")
        if b:
            kl.append(_read_csv_zip(b))
        d += timedelta(days=1)
    if not kl:
        return "nodata"
    k = pd.concat([x.iloc[:, :6].set_axis(["ts", "o", "h", "l", "c", "v"], axis=1) for x in kl])
    k = k[pd.to_numeric(k["ts"], errors="coerce").notna()]
    k["ts"] = _norm_ms(k["ts"])
    k = k.astype(float).drop_duplicates("ts").sort_values("ts")

    fr = []
    for m in months(start - timedelta(days=5), last_full):
        b = _get(f"{BASE}/data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{m:%Y-%m}.zip")
        if b:
            f = _read_csv_zip(b)
            fr.append(pd.DataFrame({"ts": _norm_ms(f.iloc[:, 0]), "r": f.iloc[:, -1].astype(float)}))
    f = pd.concat(fr).drop_duplicates("ts").sort_values("ts") if fr else pd.DataFrame({"ts": [], "r": []})

    oi = []
    d = oi_start
    while d <= end:
        b = _get(f"{BASE}/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{d:%Y-%m-%d}.zip")
        if b:
            x = _read_csv_zip(b)
            t = (pd.to_datetime(x["create_time"], utc=True) - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)
            oi.append(pd.DataFrame({"ts": t, "oi": x["sum_open_interest"].astype(float)}))
        d += timedelta(days=1)
    o = pd.concat(oi).drop_duplicates("ts").sort_values("ts") if oi else pd.DataFrame({"ts": [], "oi": []})

    np.savez_compressed(path, k=k.to_numpy(), f=f.to_numpy(dtype=float), o=o.to_numpy(dtype=float))
    return f"ok {len(k)} bars, {len(f)} funding, {len(o)} oi"


def cmd_fetch(a):
    os.makedirs(a.data, exist_ok=True)
    syms = list_symbols()
    if a.limit:
        syms = syms[: a.limit]
    print(f"{len(syms)} symbols", flush=True)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    oi_start = end - timedelta(days=a.oi_days + 2)
    t0, n = time.time(), 0
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {s: ex.submit(fetch_symbol, s, start, end, oi_start, a.data) for s in syms}
        for s, fu in futs.items():
            n += 1
            try:
                r = fu.result()
            except Exception as e:  # keep going on one bad symbol
                r = f"error {e!r}"
            if n % 50 == 0 or r.startswith("error"):
                print(f"[{n}/{len(syms)}] {s}: {r} ({time.time()-t0:.0f}s)", flush=True)


# ---------------------------------------------------------------- features & signals


def load(path: str):
    z = np.load(path)
    k = pd.DataFrame(z["k"], columns=["ts", "o", "h", "l", "c", "v"])
    k["ts"] = k["ts"].astype("int64")
    k = k.set_index("ts")
    # reindex on a regular 4h grid so shifts are time-correct
    idx = np.arange(k.index[0], k.index[-1] + 1, BAR_MS)
    k = k.reindex(idx)
    f = pd.DataFrame(z["f"], columns=["ts", "r"]) if z["f"].size else pd.DataFrame(columns=["ts", "r"])
    o = pd.DataFrame(z["o"], columns=["ts", "oi"]) if z["o"].size else pd.DataFrame(columns=["ts", "oi"])
    if len(o) and o["ts"].max() < 10**11:  # stored in seconds by an older fetch (pandas 3 datetime unit)
        o["ts"] = o["ts"] * 1000
    return k, f, o


def features(k: pd.DataFrame, f: pd.DataFrame, o: pd.DataFrame) -> pd.DataFrame:
    c, h, l, v = k["c"], k["h"], k["l"], k["v"]
    x = pd.DataFrame(index=k.index)
    x["ret7d"] = c / c.shift(42) - 1
    x["near_low"] = c <= l.rolling(84, min_periods=84).min() * 1.08
    x["bleed"] = (x["ret7d"] <= -0.15) & x["near_low"]
    # funding: mean of settlements in (t_close-24h, t_close]; bar index = open time, close = +4h
    close_t = k.index.values + BAR_MS
    if len(f):
        ft, fr = f["ts"].values.astype("int64"), f["r"].values
        cs = np.concatenate([[0.0], np.cumsum(fr)])
        hi = np.searchsorted(ft, close_t, side="right")
        lo = np.searchsorted(ft, close_t - 24 * 3_600_000, side="right")
        cnt = hi - lo
        with np.errstate(invalid="ignore", divide="ignore"):
            fm = np.where(cnt > 0, (cs[hi] - cs[lo]) / np.maximum(cnt, 1), np.nan)
        # no funding data after archive end -> NaN instead of stale
        fm[close_t > ft[-1] + 9 * 3_600_000] = np.nan
        x["fund24"] = fm
    else:
        x["fund24"] = np.nan
    x["p_fund"] = x["fund24"] <= -0.0003
    v8 = v.rolling(2).sum()
    v8avg = v.rolling(42, min_periods=42).mean().shift(2) * 2
    x["p_vol"] = (v8 >= 3 * v8avg) & ((c / c.shift(2) - 1).abs() <= 0.03)
    mid = c.rolling(20).mean()
    bbw = 4 * c.rolling(20).std() / mid
    x["bbw_pct"] = bbw.rolling(180, min_periods=120).rank(pct=True)
    x["p_bb"] = x["bbw_pct"] <= 0.10
    x["p_any"] = x["p_fund"] | x["p_vol"] | x["p_bb"]
    # OI at bar close (last 5m sample <= close time)
    if len(o):
        ot, ov = o["ts"].values.astype("int64"), o["oi"].values
        j = np.searchsorted(ot, close_t, side="right") - 1
        ok = (j >= 0) & (close_t - ot[np.maximum(j, 0)] <= 30 * 60_000)
        oi = pd.Series(np.where(ok, ov[np.maximum(j, 0)], np.nan), index=k.index)
        x["oi24"] = oi / oi.shift(6) - 1
    else:
        x["oi24"] = np.nan
    x["px24"] = c / c.shift(6) - 1
    x["p_oi"] = (x["oi24"] >= 0.10) & x["px24"].between(-0.12, 0.03)
    # entry trigger
    x["brk"] = (c > h.shift(1).rolling(6).max()) & (v >= 1.5 * v.shift(1).rolling(20).mean())
    x["sl_ref"] = l.rolling(6).min()
    return x


def simulate(k: pd.DataFrame, x: pd.DataFrame, watch: np.ndarray, t_from: int, t_to: int,
             window: int = 6, rr: float = 3.0, hold: int = 18, cost: float = 0.002,
             sl_min: float = 0.03, sl_max: float = 0.08) -> list[dict]:
    """watch: bool array (bar flags). window=0 -> enter on brk bars directly (plain breakout)."""
    ts = k.index.values
    c, h, l = k["c"].values, k["h"].values, k["l"].values
    brk, slr = x["brk"].values, x["sl_ref"].values
    n = len(ts)
    if window > 0:
        w = pd.Series(watch.astype(float)).rolling(window + 1, min_periods=1).max().values > 0
        # watch must be at or before entry bar, within `window` bars; allow same bar
    else:
        w = np.ones(n, bool)
    trades, i = [], 0
    while i < n - 1:
        if not (brk[i] and w[i] and t_from <= ts[i] < t_to) or np.isnan(c[i]):
            i += 1
            continue
        e = c[i]
        dist = min(max(1 - slr[i] / e, sl_min), sl_max) if not np.isnan(slr[i]) else sl_min
        sl, tp = e * (1 - dist), e * (1 + rr * dist)
        res, j = None, i + 1
        while j < n and j <= i + hold:
            if np.isnan(c[j]):
                j += 1
                continue
            if l[j] <= sl:
                res = (sl, "sl")
                break
            if h[j] >= tp:
                res = (tp, "tp")
                break
            j += 1
        if res is None:
            why = "open" if j >= n else "time"
            j = min(j, n - 1)
            while j > i and np.isnan(c[j]):
                j -= 1
            res = (c[j], why)
        px, why = res
        r = ((px / e - 1) - cost) / dist
        trades.append({"t": int(ts[i]), "exit_t": int(ts[j]), "r": r, "why": why, "dist": dist,
                       "ret": px / e - 1})
        i = j + 1
    return trades


def stats(tr: pd.DataFrame) -> dict:
    if not len(tr):
        return {"n": 0}
    r = tr.sort_values("t")["r"]
    eq = r.cumsum()
    dd = (eq - eq.cummax()).min()
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    return {"n": int(len(r)), "win": round(float((r > 0).mean()), 3),
            "tp_rate": round(float((tr["why"] == "tp").mean()), 3),
            "avgR": round(float(r.mean()), 3), "medR": round(float(r.median()), 3),
            "sumR": round(float(r.sum()), 1), "PF": round(float(pos / neg), 2) if neg else None,
            "maxDD_R": round(float(dd), 1),
            "avgR_ex_top1pct": round(float(r.sort_values().iloc[: max(1, int(len(r) * 0.99))].mean()), 3),
            "syms": int(tr["sym"].nunique())}


def cmd_run(a):
    files = sorted(f for f in os.listdir(a.data) if f.endswith(".npz"))
    t_from = int(datetime.fromisoformat(a.__dict__["from"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
    mid = t_from + int(182.5 * 86_400_000)
    allt: dict[str, list] = {}
    t_last = 0
    btc = None
    bk, _, _ = load(os.path.join(a.data, "BTCUSDT.npz"))
    bc = bk["c"].ffill()
    regime = (bc > bc.rolling(120).mean())  # BTC above its 20-day SMA at bar close
    reg_t, reg_v = regime.index.values, regime.values
    for fn in files:
        sym = fn[:-4]
        try:
            k, f, o = load(os.path.join(a.data, fn))
        except Exception as e:
            print("load fail", sym, e)
            continue
        if len(k) < 120:
            continue
        x = features(k, f, o)
        t_last = max(t_last, int(k.index[-1]))
        if sym == "BTCUSDT":
            btc = k["c"]
        end = 2**62
        variants = {
            "A_bleed+any": x["bleed"] & x["p_any"],
            "bleed+fund": x["bleed"] & x["p_fund"],
            "bleed+vol": x["bleed"] & x["p_vol"],
            "bleed+bb": x["bleed"] & x["p_bb"],
            "bleed_only": x["bleed"],
        }
        for name, wv in variants.items():
            for t in simulate(k, x, wv.fillna(False).values, t_from, end):
                allt.setdefault(name, []).append({**t, "sym": sym})
        for t in simulate(k, x, np.ones(len(k), bool), t_from, end, window=0):
            allt.setdefault("plain_breakout", []).append({**t, "sym": sym})
        # OI variants: last 30 days only
        oi_from = int(k.index[-1]) - a.oi_days * 86_400_000
        for name, wv in {"OI(oi24>=10%,px24 -12..+3%)": x["p_oi"],
                         "OI+7d<=-15%": x["p_oi"] & (x["ret7d"] <= -0.15)}.items():
            for t in simulate(k, x, wv.fillna(False).values, oi_from, end):
                allt.setdefault(name, []).append({**t, "sym": sym})
        for t in simulate(k, x, np.ones(len(k), bool), oi_from, end, window=0):
            allt.setdefault("plain_breakout_last30d", []).append({**t, "sym": sym})
        # how many watch bars, for context
        allt.setdefault("_watchcount", []).append(
            {"sym": sym, "bleed": int(x["bleed"].sum()), "A": int((x["bleed"] & x["p_any"]).sum()),
             "fund_nan_tail": int(x["fund24"].isna().sum())})

    for name in ("A_bleed+any", "bleed_only", "bleed+vol", "plain_breakout"):
        lst = allt.get(name, [])
        keep = [t for t in lst if reg_v[min(max(np.searchsorted(reg_t, t["t"], side="right") - 1, 0), len(reg_v) - 1)]]
        allt[name + "|BTC>SMA20d"] = keep
    os.makedirs(a.out, exist_ok=True)
    report = {"generated": datetime.now(timezone.utc).isoformat(), "symbols": len(files),
              "from": a.__dict__["from"], "half_split": datetime.fromtimestamp(mid / 1000, timezone.utc).date().isoformat(),
              "data_end": datetime.fromtimestamp((t_last + BAR_MS) / 1000, timezone.utc).isoformat(), "variants": {}}
    if btc is not None:
        b = btc.dropna()
        def bret(t0, t1):
            s = b[(b.index >= t0) & (b.index < t1)]
            return round(float(s.iloc[-1] / s.iloc[0] - 1), 3) if len(s) > 1 else None
        report["btc_return"] = {"H1": bret(t_from, mid), "H2": bret(mid, 2**62)}
    for name, lst in allt.items():
        if name.startswith("_"):
            continue
        tr = pd.DataFrame(lst)
        tr.to_csv(os.path.join(a.out, f"trades_{re.sub(r'[^A-Za-z0-9_+.-]', '_', name)}.csv"), index=False)
        rep = {"all": stats(tr)}
        if not len(tr):
            report["variants"][name] = {"all": {"n": 0}}
            continue
        if not name.startswith("OI") and not name.endswith("last30d"):
            rep["H1"] = stats(tr[tr["t"] < mid])
            rep["H2"] = stats(tr[tr["t"] >= mid])
            q = pd.to_datetime(tr["t"], unit="ms").dt.to_period("Q").astype(str)
            rep["by_quarter"] = {p: stats(g) for p, g in tr.groupby(q)}
        report["variants"][name] = rep
    wc = pd.DataFrame(allt.get("_watchcount", []))
    if len(wc):
        report["watch_bars_total"] = {"bleed": int(wc["bleed"].sum()), "A": int(wc["A"].sum())}
    with open(os.path.join(a.out, "summary.json"), "w") as fh:
        json.dump(report, fh, indent=1)
    # compact log table
    print(f"\nsymbols={len(files)} from={report['from']} split={report['half_split']} end={report['data_end']}")
    print("btc", report.get("btc_return"))
    print(f"{'variant':34s} {'seg':4s} {'n':>6s} {'win':>6s} {'avgR':>7s} {'medR':>6s} {'sumR':>8s} {'PF':>5s} {'DD':>7s} {'ex1%':>7s}")
    for name, rep in report["variants"].items():
        for seg in ("all", "H1", "H2"):
            s = rep.get(seg)
            if not s or not s.get("n"):
                if seg == "all":
                    print(f"{name:34s} all  0 trades")
                continue
            print(f"{name:34s} {seg:4s} {s['n']:6d} {s['win']:6.3f} {s['avgR']:7.3f} {s['medR']:6.2f} "
                  f"{s['sumR']:8.1f} {str(s['PF']):>5s} {s['maxDD_R']:7.1f} {s['avgR_ex_top1pct']:7.3f}")


def main():
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("fetch")
    f.add_argument("--start", required=True)
    f.add_argument("--end", required=True)
    f.add_argument("--data", default="data/bt")
    f.add_argument("--oi-days", type=int, default=32)
    f.add_argument("--workers", type=int, default=24)
    f.add_argument("--limit", type=int, default=0)
    r = sp.add_parser("run")
    r.add_argument("--data", default="data/bt")
    r.add_argument("--from", required=True)
    r.add_argument("--out", default="results")
    r.add_argument("--oi-days", type=int, default=30)
    a = p.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
