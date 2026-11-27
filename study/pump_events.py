"""최근 60일 +30% 급등 이벤트 추출 + 급등 직전(론치 시점) 차트 특징 vs 일반 시점 비교.

이벤트 정의 (1h 봉):
  - 어떤 봉 종가 대비 이후 72시간 고가가 +30% 이상이면 '급등 구간' 후보
  - 그 구간의 고점(peak) = 후보 시작 후 72h 안 최고 고가
  - 론치(launch) = peak 직전 72h 안 최저 저가 봉 → 상승폭 = peak/launch_low - 1 (>= 30%)
  - 같은 종목은 peak 이후 72h 동안 새 이벤트를 만들지 않음
특징은 론치 봉 마감 시점까지의 데이터만 사용.
"""
import glob, os, sys
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D1H = f"{ROOT}/data/fut_1h"
EV_FROM, EV_TO = pd.Timestamp("2026-09-26"), pd.Timestamp("2026-11-25 23:00")
H, PUMP = 72, 0.30

def rsi(c, n=14):
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + up/dn)

def to4h(df):
    return df.resample("4h").agg({"o":"first","h":"max","l":"min","c":"last","v":"sum","qv":"sum","tqv":"sum"}).dropna()

def features(df, btc):
    """1h df -> 시점별 특징 DataFrame (각 행은 해당 1h 봉 마감 시점 기준)."""
    c, h, l, v, qv = df.c, df.h, df.l, df.v, df.qv
    f = pd.DataFrame(index=df.index)
    for n, lab in [(24,"1d"),(72,"3d"),(168,"7d"),(720,"30d")]:
        f[f"ret_{lab}"] = c/c.shift(n) - 1
    f["dist_30d_high"] = c/h.rolling(720, min_periods=240).max() - 1
    f["dist_30d_low"]  = c/l.rolling(720, min_periods=240).min() - 1
    f["dist_14d_low"]  = c/l.rolling(336, min_periods=120).min() - 1
    # 4h 지표를 1h 격자에 (완성된 4h 봉만) 붙임
    d4 = to4h(df); c4 = d4.c
    x4 = pd.DataFrame(index=d4.index + pd.Timedelta("4h"))   # 4h 봉 마감 시각
    x4["rsi_4h"] = rsi(c4).values
    x4["c_vs_ema20_4h"] = (c4/c4.ewm(span=20).mean() - 1).values
    x4["c_vs_ema50_4h"] = (c4/c4.ewm(span=50).mean() - 1).values
    ma, sd = c4.rolling(20).mean(), c4.rolling(20).std()
    bbw = (4*sd/ma)
    x4["bbw_4h"] = bbw.values
    x4["bbw_4h_pct30d"] = bbw.rolling(180, min_periods=60).rank(pct=True).values
    x4["vol_mult_4h"] = (d4.v / d4.v.rolling(42).mean().shift(1)).values      # 직전 7일 평균 대비
    x4["taker_buy_4h"] = (d4.tqv/d4.qv).values
    f = f.join(x4.shift(0), how="left")
    f[x4.columns] = f[x4.columns].ffill()
    # 1h 지표
    f["rsi_1h"] = rsi(c)
    f["vol_mult_8h"] = v.rolling(8).sum() / (v.rolling(168).sum()/21).shift(8)
    f["range_48h"] = h.rolling(48).max()/l.rolling(48).min() - 1
    f["atr_pct_1h"] = (pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)/c).rolling(24).mean()
    f["qv_24h_musd"] = qv.rolling(24).sum()/1e6
    f["taker_buy_24h"] = df.tqv.rolling(24).sum()/qv.rolling(24).sum()
    f["listing_age_d"] = (df.index - df.index[0]).days  # 8/1 이전 상장은 >=, 하한값
    f["new_listing"] = df.index[0] > pd.Timestamp("2026-08-02")
    f["btc_ret_1d"] = (btc/btc.shift(24) - 1).reindex(df.index)
    f["btc_ret_7d"] = (btc/btc.shift(168) - 1).reindex(df.index)
    # 이전 분석 규칙 신호
    f["bleed"] = (f.ret_7d <= -0.15) & (f.dist_14d_low <= 0.08)
    f["quiet_vol"] = (f.vol_mult_8h >= 3) & ((c/c.shift(8)-1).abs() <= 0.03)
    f["bb_squeeze"] = f.bbw_4h_pct30d <= 0.10
    f["above_ema50_4h"] = f.c_vs_ema50_4h > 0
    return f

def find_events(df):
    c, h, l = df.c.values, df.h.values, df.l.values
    n = len(df); idx = df.index
    fwd = pd.Series(h[::-1]).rolling(H, min_periods=1).max()[::-1].shift(-1).values  # max high next 72h
    gain = fwd/c - 1
    ev, t = [], 0
    while t < n - 1:
        if idx[t] < EV_FROM - pd.Timedelta(hours=H) or not (gain[t] >= PUMP):
            t += 1; continue
        p = t + 1 + int(np.argmax(h[t+1:t+1+H]))          # peak
        # 고점이 더 이어지면 확장 (peak 이후 72h 안에 더 높은 고점)
        while True:
            seg = h[p+1:p+1+H]
            if len(seg) and seg.max() > h[p] * 1.0: p = p + 1 + int(np.argmax(seg))
            else: break
        lo = max(0, p - H); L = lo + int(np.argmin(l[lo:p+1]))
        g = h[p]/l[L] - 1
        if g >= PUMP and idx[L] >= EV_FROM and idx[p] <= EV_TO:
            after = slice(p+1, min(n, p+1+H))
            low_after = l[after].min() if after.stop > after.start else np.nan
            c_after = c[min(n-1, p+H)]
            # 상승폭의 절반(=론치~고점 중간) 처음 넘은 시각, +10% 처음 넘은 시각
            run = h[L:p+1]
            t10 = int(np.argmax(run >= l[L]*1.10))
            ev.append(dict(launch=idx[L], peak=idx[p], launch_low=l[L], peak_high=h[p], gain=g,
                           gain_close=c[L:p+1].max()/c[L]-1,
                           hours_to_peak=p-L, hours_to_10pct=t10,
                           dd_after_peak_72h=low_after/h[p]-1 if low_after==low_after else np.nan,
                           close_72h_after_vs_launch=c_after/l[L]-1,
                           retained=(c_after-l[L])/(h[p]-l[L]),
                           still_rising=(idx[p] > EV_TO - pd.Timedelta(hours=H))))
        t = p + H
    return ev

def valid_mask(df):
    """직전 30일~이후 72h 안에 거래량 0 봉(거래정지/상장폐지/재상장)이 없는 시점만 True."""
    z = (df.qv <= 0).astype(int)
    past = z.rolling(720, min_periods=1).sum()
    fut = z[::-1].rolling(H + 1, min_periods=1).sum()[::-1]
    return (past == 0) & (fut == 0)

def main():
    btc = pd.read_pickle(f"{D1H}/BTCUSDT.pkl").c
    rows, base = [], []
    rng = np.random.default_rng(0)
    files = sorted(glob.glob(f"{D1H}/*.pkl"))
    for fp in files:
        sym = os.path.basename(fp)[:-4]
        df = pd.read_pickle(fp)
        if len(df) < 24*14: continue
        f = features(df, btc)
        ok = valid_mask(df)
        if not ok.any(): continue
        evs = [e for e in find_events(df) if ok[e["launch"]] and ok[e["peak"]]]
        for e in evs:
            r = dict(symbol=sym, **e); r.update(f.loc[e["launch"]].to_dict()); rows.append(r)
        # 기준 표본: 이벤트 구간(론치-72h ~ 고점+72h) 밖, 4시간마다
        mask = ok.values & (f.index >= EV_FROM) & (f.index <= EV_TO - pd.Timedelta(hours=H))
        for e in evs:
            mask &= ~((f.index >= e["launch"] - pd.Timedelta(hours=H)) & (f.index <= e["peak"] + pd.Timedelta(hours=H)))
        b = f[mask & (f.index.hour % 4 == 3)].copy(); b["symbol"] = sym
        base.append(b)
    ev = pd.DataFrame(rows).sort_values("launch")
    bs = pd.concat(base)
    os.makedirs(f"{ROOT}/study/out", exist_ok=True)
    ev.to_csv(f"{ROOT}/study/out/pump_events.csv", index=False)
    bs.to_pickle(f"{ROOT}/data/baseline_features.pkl")
    print(f"symbols {len(files)}, events {len(ev)} ({ev.symbol.nunique()} symbols), baseline rows {len(bs)}")

if __name__ == "__main__":
    main()
