"""2026-11-25 마감 기준, 최근 7일 저점 대비 +30% 이상 올라 있는 종목."""
import glob, os, sys, pandas as pd
sys.path.insert(0, os.path.dirname(__file__)); from pump_events import valid_mask
from tradfi import is_tradfi
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
rows = []
for fp in glob.glob(f"{ROOT}/data/fut_1h/*.pkl"):
    if is_tradfi(os.path.basename(fp)[:-4]): continue
    d = pd.read_pickle(fp)
    if len(d) < 200 or d.qv.iloc[-24:].sum() <= 0 or (d.qv.iloc[-168:] <= 0).any(): continue
    w = d.iloc[-168:]; c = d.c.iloc[-1]
    lo = w.l.min(); t_lo = w.l.idxmin()
    rows.append(dict(symbol=os.path.basename(fp)[:-4], close=c, from_7d_low=c/lo-1, low_time=t_lo,
                     ret_7d=c/d.c.iloc[-169]-1, ret_24h=c/d.c.iloc[-25]-1,
                     off_7d_high=c/w.h.max()-1, qv24_musd=d.qv.iloc[-24:].sum()/1e6))
x = pd.DataFrame(rows).sort_values("from_7d_low", ascending=False)
x = x[x.from_7d_low >= 0.30]
L = ["## 6. 현재(11-25 마감) 7일 저점 대비 +30% 이상인 종목", "",
     "| 종목 | 7일 저점 대비 | 7일 수익률 | 24h | 7일 고점 대비 | 저점 시각 | 24h 거래대금 $M |", "|---|---|---|---|---|---|---|"]
for _, r in x.iterrows():
    L.append(f"| {r.symbol} | {r.from_7d_low:+.0%} | {r.ret_7d:+.0%} | {r.ret_24h:+.0%} | {r.off_7d_high:+.0%} | {r.low_time:%m-%d %H} | {r.qv24_musd:.1f} |")
open(f"{ROOT}/study/out/current.md","w").write("\n".join(L)); print("\n".join(L))
