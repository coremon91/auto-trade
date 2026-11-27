"""'이미 오르기 시작한 종목'을 따라 들어가면 어떻게 되나 (생존 편향 없는 기준율).

트리거: 1h 종가가 직전 24h 최저가 대비 +X% 이상이 처음 된 봉 (종목별 24h 쿨다운).
그 종가에 진입했다고 보고 이후 72h 최고/최저, 24h/72h 후 종가, 그리고 저점 기준 +30% 도달 여부.
"""
import glob, os, sys, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from pump_events import valid_mask
from tradfi import is_tradfi
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROM, TO = pd.Timestamp("2026-09-26"), pd.Timestamp("2026-11-22 23:00")
rows = []
for fp in sorted(glob.glob(f"{ROOT}/data/fut_1h/*.pkl")):
    sym = os.path.basename(fp)[:-4]; df = pd.read_pickle(fp)
    if is_tradfi(sym): continue
    if len(df) < 24*14: continue
    c, h, l, v = df.c, df.h, df.l, df.v
    ok = valid_mask(df)
    low24 = l.rolling(24).min()
    up = c/low24 - 1
    vm = v/v.rolling(168).mean().shift(1)
    fh = h[::-1].rolling(72, min_periods=1).max()[::-1].shift(-1)
    fl = l[::-1].rolling(72, min_periods=1).min()[::-1].shift(-1)
    c24, c72 = c.shift(-24), c.shift(-72)
    for X in (0.10, 0.15, 0.20):
        trig = (up >= X) & (up.shift(1) < X)
        last = None
        for t in up.index[trig & ok & (up.index >= FROM) & (up.index <= TO)]:
            if last is not None and t - last < pd.Timedelta(hours=24): continue
            last = t
            rows.append(dict(symbol=sym, t=t, X=X, up=up[t], vol_mult_1h=vm[t],
                             mfe=fh[t]/c[t]-1, mae=fl[t]/c[t]-1, r24=c24[t]/c[t]-1, r72=c72[t]/c[t]-1,
                             hit30_from_low=fh[t]/low24[t]-1 >= 0.30))
d = pd.DataFrame(rows)
d.to_csv(f"{ROOT}/study/out/chase_triggers.csv", index=False)
L = ["## 4. 이미 움직이기 시작한 종목을 따라 사면? (전체 트리거 기준, 생존 편향 없음)",
     "트리거 = 1h 종가가 직전 24h 최저가 대비 +X% 처음 돌파. 진입가 = 그 봉 종가, 수수료 미반영.", "",
     "| X | 트리거 수 | 저점 대비 +30% 도달 | 72h 내 추가 +10% 도달 | 72h 내 -10% 도달 | 24h 후 수익률 중앙값 | 72h 후 중앙값 | 72h 후 평균 |",
     "|---|---|---|---|---|---|---|---|"]
for X, g in d.groupby("X"):
    L.append(f"| +{X:.0%} | {len(g)} | {g.hit30_from_low.mean():.0%} | {(g.mfe>=.10).mean():.0%} | {(g.mae<=-.10).mean():.0%} | {g.r24.median():+.1%} | {g.r72.median():+.1%} | {g.r72.mean():+.1%} |")
L += ["", "거래량 동반 여부 (+15% 트리거, 트리거 봉 거래량 / 직전 7일 1h 평균):", "",
      "| 거래량 배수 | 트리거 수 | +30% 도달 | 72h 후 중앙값 | 72h 후 평균 |", "|---|---|---|---|---|"]
g = d[d.X==0.15]
for lab, m in [("< 3x", g.vol_mult_1h<3), ("3~10x", g.vol_mult_1h.between(3,10)), ("≥ 10x", g.vol_mult_1h>10)]:
    s = g[m]; L.append(f"| {lab} | {len(s)} | {s.hit30_from_low.mean():.0%} | {s.r72.median():+.1%} | {s.r72.mean():+.1%} |")
open(f"{ROOT}/study/out/chase.md","w").write("\n".join(L)); print("\n".join(L))
