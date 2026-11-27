"""15분 점화 봉(거래량 5배↑ 양봉, 종가가 봉 상단 60%↑) 전체에 대한 결과 (급등 여부와 무관)."""
import glob, os, sys, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__)); from tradfi import is_tradfi
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROM, TO = pd.Timestamp("2026-09-26"), pd.Timestamp("2026-11-22 23:00")
rows = []
for fp in glob.glob(f"{ROOT}/data/fut_15m/*.pkl"):
    sym = os.path.basename(fp)[:-4]
    if is_tradfi(sym): continue
    d = pd.read_pickle(fp)
    if (d.qv <= 0).any(): d = d[d.qv > 0]
    va = d.v.rolling(96).mean().shift(1)
    vm = d.v/va; pos = (d.c-d.l)/(d.h-d.l+1e-12)
    low24 = d.l.rolling(96).min()
    N = 288  # 72h
    fh = d.h[::-1].rolling(N, min_periods=1).max()[::-1].shift(-1)
    fl = d.l[::-1].rolling(N, min_periods=1).min()[::-1].shift(-1)
    c72 = d.c.shift(-N)
    sig = (vm >= 5) & (d.c > d.o) & (pos >= 0.6) & (d.index >= FROM) & (d.index <= TO)
    last = None
    for t in d.index[sig]:
        if last is not None and t - last < pd.Timedelta("24h"): continue
        last = t; c = d.c[t]
        rows.append(dict(symbol=sym, t=t, vm=vm[t], moved=c/low24[t]-1, mfe=fh[t]/c-1, mae=fl[t]/c-1,
                         r72=c72[t]/c-1, hit30=fh[t]/low24[t]-1 >= 0.30))
x = pd.DataFrame(rows)
L = ["", "### 생존 편향 확인: 같은 종목들의 모든 15분 점화 봉 (급등 여부 무관, 종목별 24h 쿨다운)", "",
     "| 점화 시점에 이미 오른 폭 (24h 저점 대비) | 신호 수 | 저점 대비 +30% 도달 | 72h 내 +20% 추가 | 72h 내 -10% 역행 | 72h 후 중앙값 | 72h 후 평균 |",
     "|---|---|---|---|---|---|---|"]
for lab, m in [("< 5%", x.moved < .05), ("5~10%", x.moved.between(.05,.10)), ("10~20%", x.moved.between(.10,.20)), ("≥ 20%", x.moved > .20), ("전체", x.moved > -1)]:
    s = x[m]; L.append(f"| {lab} | {len(s)} | {s.hit30.mean():.0%} | {(s.mfe>=.2).mean():.0%} | {(s.mae<=-.1).mean():.0%} | {s.r72.median():+.1%} | {s.r72.mean():+.1%} |")
open(f"{ROOT}/study/out/m15.md","a").write("\n".join(L)); print("\n".join(L))
