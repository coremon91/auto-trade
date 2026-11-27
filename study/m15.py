"""15분봉으로 본 급등 과정: 점화 봉, 상승 중 눌림, 고점 형태."""
import os, numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ev = pd.read_csv(f"{ROOT}/study/out/pump_events.csv", parse_dates=["launch","peak"])
ev = ev[ev.gain_close >= 0.30]
import sys; sys.path.insert(0, os.path.dirname(__file__)); from tradfi import is_tradfi
ev = ev[~ev.symbol.map(is_tradfi)]
rows = []
for _, e in ev.iterrows():
    fp = f"{ROOT}/data/fut_15m/{e.symbol}.pkl"
    if not os.path.exists(fp): continue
    d = pd.read_pickle(fp)
    vavg = d.v.rolling(96).mean().shift(1)              # 직전 24h 15m 평균 거래량
    run = d.loc[e.launch: e.peak + pd.Timedelta("45min")]
    if len(run) < 2: continue
    lo = run.l.min(); t_lo = run.l.idxmin(); run = run.loc[t_lo:]
    hi = run.h.max(); t_hi = run.h.idxmax(); run = run.loc[:t_hi]
    vm = (run.v / vavg.reindex(run.index))
    ign = vm[(vm >= 5) & (run.c > run.o)]
    t_ig = ign.index[0] if len(ign) else None
    pre = d.loc[t_lo - pd.Timedelta("6h"): t_lo]
    runhi = run.h.cummax(); pull = (run.l/runhi - 1)
    after = d.loc[t_hi: t_hi + pd.Timedelta("24h")]
    r = dict(symbol=e.symbol, launch=t_lo, peak=t_hi, gain=hi/lo-1, bars=len(run),
             pre6h_range=pre.h.max()/pre.l.min()-1 if len(pre) else np.nan,
             max_pullback=pull.min(),
             green_share=(run.c > run.o).mean(),
             big_bars=int(((run.c/run.o-1) >= 0.05).sum()),
             top_bar_share=((run.c/run.o-1).max())/(hi/lo-1),
             peak_vol_mult=vm.max(),
             peak_bar_wick=(d.loc[t_hi,"h"]-max(d.loc[t_hi,"o"],d.loc[t_hi,"c"]))/(d.loc[t_hi,"h"]-d.loc[t_hi,"l"]+1e-12),
             drop_24h_after=after.l.min()/hi-1 if len(after) else np.nan)
    if t_ig is not None:
        b = d.loc[t_ig]
        r.update(ign_delay_min=(t_ig - t_lo).total_seconds()/60, ign_vol_mult=vm[t_ig],
                 moved_at_ign=b.c/lo-1, left_after_ign=hi/b.c-1,
                 ign_body=b.c/b.o-1, ign_close_pos=(b.c-b.l)/(b.h-b.l+1e-12), ign_taker=b.tqv/b.qv,
                 dd_after_ign=d.loc[t_ig:t_hi].l.min()/b.c-1)
    rows.append(r)
m = pd.DataFrame(rows); m.to_csv(f"{ROOT}/study/out/m15_events.csv", index=False)
q = lambda s: f"{s.quantile(.25):.2f} / **{s.median():.2f}** / {s.quantile(.75):.2f}"
L = ["## 5. 15분봉으로 본 급등 과정", f"대상 {len(m)}건. 점화 봉 = 저점 이후 첫 양봉 중 거래량이 직전 24h 15분 평균의 5배 이상인 봉.", "",
     "| 항목 | 25% / 중앙값 / 75% |", "|---|---|",
     f"| 저점 직전 6h 고저폭 | {q(m.pre6h_range)} |",
     f"| 상승 중 양봉 비율 | {q(m.green_share)} |",
     f"| 상승 중 최대 눌림 (고점 갱신 중 최고가 대비) | {q(m.max_pullback)} |",
     f"| 가장 큰 15분 양봉이 전체 상승에서 차지하는 비중 | {q(m.top_bar_share)} |",
     f"| 상승 중 최대 거래량 배수 | {q(m.peak_vol_mult)} |",
     f"| 고점 봉 윗꼬리 비율 (봉 길이 대비) | {q(m.peak_bar_wick)} |",
     f"| 고점 후 24h 최대 낙폭 | {q(m.drop_24h_after)} |"]
g = m.dropna(subset=["ign_delay_min"])
L += ["", f"점화 봉이 있었던 이벤트: {len(g)}/{len(m)} ({len(g)/len(m):.0%})", "",
      "| 점화 봉 | 25% / 중앙값 / 75% |", "|---|---|",
      f"| 저점→점화 경과 (분) | {q(g.ign_delay_min)} |",
      f"| 점화 봉 거래량 배수 | {q(g.ign_vol_mult)} |",
      f"| 점화 봉 종가 시점 이미 오른 폭 | {q(g.moved_at_ign)} |",
      f"| 점화 봉 종가 → 고점 남은 폭 | {q(g.left_after_ign)} |",
      f"| 점화 봉 몸통 상승률 | {q(g.ign_body)} |",
      f"| 점화 봉 종가 위치 (0=저가, 1=고가) | {q(g.ign_close_pos)} |",
      f"| 점화 봉 테이커 매수 비중 | {q(g.ign_taker)} |",
      f"| 점화 봉 종가 진입 시 고점 전 최대 역행 | {q(g.dd_after_ign)} |"]
open(f"{ROOT}/study/out/m15.md","w").write("\n".join(L)); print("\n".join(L))
