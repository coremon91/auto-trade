"""pump_events.csv + baseline 비교 → study/out/report.md"""
import os, numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = f"{ROOT}/study/out"
import sys; sys.path.insert(0, os.path.dirname(__file__)); from tradfi import is_tradfi
ev = pd.read_csv(f"{OUT}/pump_events.csv", parse_dates=["launch","peak"])
bs = pd.read_pickle(f"{ROOT}/data/baseline_features.pkl")
ev = ev[~ev.symbol.map(is_tradfi)]; bs = bs[~bs.symbol.map(is_tradfi)]
ev_all = ev
ev = ev[ev.gain_close >= 0.30]          # 꼬리(윅)만이 아닌, 종가 기준으로도 +30%
L = []
def w(s=""): L.append(s)

w("# 최근 60일 +30% 급등 차트 특징 (바이낸스 USDT-M 무기한 선물)")
w(f"- 기간: 2026-09-26 ~ 2026-11-25 (UTC), 1h 봉, 현재 거래 중인 코인 {bs.symbol.nunique()}개 (주식·ETF·원자재 선물 제외)")
w(f"- 이벤트: 72h 안에 저점→고점 +30% (고가 기준 {len(ev_all)}건) 중 종가 기준으로도 +30%인 **{len(ev)}건, {ev.symbol.nunique()}종목**")
w(f"- 비교 기준: 이벤트 구간 밖의 일반 시점 {len(bs):,}개 (4시간 간격)")
w()
w("## 1. 급등 모양")
q = lambda s: f"{s.quantile(.25):.2f} / **{s.median():.2f}** / {s.quantile(.75):.2f}"
w("| 항목 | 25% / 중앙값 / 75% |"); w("|---|---|")
w(f"| 상승폭 (저점→고점 고가) | {q(ev.gain)} |")
w(f"| 저점→고점 소요시간 (h) | {q(ev.hours_to_peak)} |")
w(f"| 저점에서 +10% 도달까지 (h) | {q(ev.hours_to_10pct)} |")
done = ev[~ev.still_rising]
w(f"| 고점 후 72h 최대 낙폭 | {q(done.dd_after_peak_72h)} |")
w(f"| 고점 72h 후 종가, 상승분 중 유지 비율 | {q(done.retained)} |")
w()
w(f"- 고점 72h 후에 상승분의 절반 이상을 반납한 비율: **{(done.retained<0.5).mean():.0%}**, 저점 아래로 되돌아간 비율: {(done.retained<0).mean():.0%}")
w(f"- 24h 안에 고점을 찍은 비율: {(ev.hours_to_peak<=24).mean():.0%}, 같은 종목 반복 급등(60일 안 2회 이상): {(ev.symbol.value_counts()>=2).sum()}종목 / 이벤트의 {ev.symbol.map(ev.symbol.value_counts()).ge(2).mean():.0%}")
w()

w("## 2. 론치(저점) 시점의 특징 vs 일반 시점")
w("값은 중앙값. 'lift' = 해당 조건일 때 급등 이벤트 비중 ÷ 전체 평균 비중 (1보다 크면 급등 직전에 더 자주 보임)")
w()
num = [("ret_7d","7일 수익률"),("ret_30d","30일 수익률"),("dist_30d_high","30일 고점 대비"),
       ("dist_30d_low","30일 저점 대비"),("rsi_4h","RSI(4h)"),("rsi_1h","RSI(1h)"),
       ("c_vs_ema50_4h","EMA50(4h) 대비"),("bbw_4h_pct30d","BB폭(4h) 30일 백분위"),
       ("range_48h","48h 고저폭"),("vol_mult_8h","8h 거래량 배수"),("vol_mult_4h","4h 거래량 배수"),
       ("taker_buy_24h","24h 테이커 매수비중"),("atr_pct_1h","1h ATR%"),("qv_24h_musd","24h 거래대금($M)"),
       ("btc_ret_1d","BTC 24h"),("btc_ret_7d","BTC 7d")]
w("| 특징 | 급등 론치 | 일반 시점 |"); w("|---|---|---|")
for k, n in num:
    w(f"| {n} | {ev[k].median():.3g} | {bs[k].median():.3g} |")
w()
# 사분위 lift (일반 시점 분포 기준 5분위)
w("### 5분위별 lift (일반 시점 분포로 구간 나눔, Q1=낮음 … Q5=높음)")
w("| 특징 | Q1 | Q2 | Q3 | Q4 | Q5 |"); w("|---|---|---|---|---|---|")
base_rate = len(ev)/(len(ev)+len(bs))
for k, n in num:
    b = bs[k].dropna(); e = ev[k].dropna()
    if len(b) < 100: continue
    edges = np.unique(b.quantile([0,.2,.4,.6,.8,1]).values); edges[0], edges[-1] = -np.inf, np.inf
    if len(edges) < 6: continue
    cb = pd.cut(b, edges).value_counts(sort=False).values; ce = pd.cut(e, edges).value_counts(sort=False).values
    lift = (ce/(ce+cb))/base_rate
    w(f"| {n} | " + " | ".join(f"{x:.2f}" for x in lift) + " |")
w()
w("### 조건 신호")
w("| 신호 | 급등 론치 비중 | 일반 시점 비중 | lift |"); w("|---|---|---|---|")
sig = [("bleed","7일 -15% 이하 + 14일 저점 8% 이내"),("bb_squeeze","4h BB폭 30일 하위 10%"),
       ("quiet_vol","8h 거래량 3배↑ + 가격 ±3% 이내"),("above_ema50_4h","4h EMA50 위"),("new_listing","신규 상장(8/2 이후)")]
for k, n in sig:
    pe, pb = ev[k].astype(float).mean(), bs[k].astype(float).mean()
    ce, cb = ev[k].astype(bool).sum(), bs[k].astype(bool).sum()
    w(f"| {n} | {pe:.0%} | {pb:.0%} | {(ce/(ce+cb))/base_rate:.2f} |")
w()
w("### 론치 시각 (UTC)")
hr = ev.launch.dt.hour.value_counts().sort_index()
w("| 시간대(UTC) | 0-3 | 4-7 | 8-11 | 12-15 | 16-19 | 20-23 |"); w("|---|---|---|---|---|---|---|")
w("| 이벤트 수 | " + " | ".join(str(int(hr[(hr.index>=a)&(hr.index<a+4)].sum())) for a in range(0,24,4)) + " |")
w()
w("## 3. 상위 급등 이벤트")
w("| 종목 | 론치(UTC) | 고점 | 상승폭 | 소요h | 고점후 72h 낙폭 | 7d수익률(론치) |"); w("|---|---|---|---|---|---|---|")
for _, r in ev.sort_values("gain", ascending=False).head(25).iterrows():
    w(f"| {r.symbol} | {r.launch:%m-%d %H} | {r.peak:%m-%d %H} | {r.gain:+.0%} | {r.hours_to_peak} | {r.dd_after_peak_72h:+.0%} | {r.ret_7d:+.0%} |")
open(f"{OUT}/report.md","w").write("\n".join(L))
print("\n".join(L))
