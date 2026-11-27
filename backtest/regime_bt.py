"""시장 국면별(scripts/periods.py) 알트 롱 전략 백테스트 — 바이낸스 USDT-M 무기한 선물.

데이터는 data.binance.vision 공개 아카이브만 사용 (상장폐지 종목 포함, API 키 불필요).
Windows / macOS / Linux 공통으로 동작.

  python backtest/regime_bt.py fetch            # 1h 봉 + 펀딩 (전 종목, 구간별 필요한 달만)
  python backtest/regime_bt.py run              # 셋업 계산 → 필요한 날짜만 15m 수집 → 시뮬레이션
  python backtest/regime_bt.py all              # 둘 다
  옵션: --fee 0.002 (왕복 수수료) --periods 1_bear,4_2_range --limit 50 (테스트용 종목 수 제한)

규칙
  유니버스 : 코인만 (주식/ETF/원자재 선물 제외), 24h 거래대금 >= $2M, 상장 7일 이상, 직전 30일 거래정지 없음
  4h 셋업  : bleed = 7일 수익률 <= -15% 이고 종가가 14일 저점 +8% 이내
             precursor(하나 이상) = 24h 평균 펀딩 <= -0.03% | 8h 거래량 >= 7일 평균의 3배이면서 8h 가격변화 ±3% 이내
                                    | 4h BB폭(20) 이 30일 하위 10%
             셋업 A = bleed + precursor, 셋업 B = bleed 만
             BTC 필터(선택) = BTC 일봉 종가 > 20일 SMA (전일 확정값)
  진입 방식
    4h  (기존 방식) : 셋업 후 24h 안에 4h 종가 > 직전 6봉 최고가 & 거래량 >= 20봉 평균 1.5배 → 그 종가에 진입
                      손절 = 직전 6봉(4h) 최저가, 청산은 1h 봉으로 시뮬레이션
    mtf (15m/1h/4h) : 셋업 후 24h 안에 1h 종가 > 1h EMA20 확인 → 그 이후 15m 종가 > 직전 24봉(6h) 최고가
                      & 15m 거래량 >= 직전 96봉 평균 3배 & 종가가 24h 저점 대비 +15% 이내(추격 금지)
                      → 다음 15m 시가 진입. 손절 = 직전 24봉(15m) 최저가, 청산은 15m 봉으로 시뮬레이션
  손절 거리는 3%~8% 로 제한.
  청산
    X1 : 손절 / 익절 3R / 최대 72시간. 같은 봉에서 손절·익절 둘 다 닿으면 손절로 처리
    X2 : 손절 / +1R 이면 손절을 본전으로 / +15% 에서 절반 익절 후 나머지는 고점 대비 -12% 트레일링 / 최대 72시간
         (스탑 갱신은 다음 봉부터 적용 — 같은 봉 고가로 올린 스탑을 같은 봉 저가로 치는 낙관 편향 방지)
  수수료 : 왕복 --fee (기본 0.2%) 를 거래마다 차감. 롱만.
  포트폴리오 : 진입 시점 자산의 10% 씩, 동시 최대 10개, 종목당 1포지션, 레버리지 없음.
"""
from __future__ import annotations

import argparse, io, json, os, re, sys, time, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "study"))
from periods import PERIODS  # noqa: E402
from tradfi import is_tradfi  # noqa: E402

BASE = "https://data.binance.vision"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DATA = os.path.join(ROOT, "data", "bt")
H1_DIR, M15_DIR = os.path.join(DATA, "h1"), os.path.join(DATA, "m15")
OUT = os.path.join(ROOT, "backtest", "results_regime")
LOOKBACK_D = 45          # 특징 계산용 구간 시작 전 여유
ARCHIVE_END = None       # 아카이브 마지막 날 (자동 탐지)

_sess = requests.Session()


# ------------------------------------------------------------------ 수집
def _get(url, tries=4):
    for a in range(tries):
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


def list_symbols():
    out, marker = [], ""
    pre = "data/futures/um/monthly/klines/"
    while True:
        t = _sess.get(f"{S3}?delimiter=/&prefix={pre}&marker={marker}", timeout=30).text
        p = re.findall(rf"<Prefix>{pre}([^/<]+)/</Prefix>", t)
        out += p
        if "<IsTruncated>true</IsTruncated>" not in t or not p:
            break
        marker = pre + p[-1] + "/"
    return sorted(s for s in set(out) if s.endswith("USDT") and "_" not in s and not is_tradfi(s))


def archive_end():
    global ARCHIVE_END
    if ARCHIVE_END is None:
        d = date.today()
        for _ in range(10):
            if _get(f"{BASE}/data/futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-{d:%Y-%m-%d}.zip", tries=2):
                break
            d -= timedelta(days=1)
        ARCHIVE_END = d
    return ARCHIVE_END


def _read_zip(b, cols):
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        raw = z.read(z.namelist()[0]).decode()
    hdr = not raw.split(",", 1)[0].strip().lstrip("-").isdigit()
    df = pd.read_csv(io.StringIO(raw), header=0 if hdr else None)
    return df.iloc[:, cols]


def _ms(s):
    s = pd.to_numeric(s, errors="coerce").astype("float64")
    return np.where(s > 1e14, s // 1000, s)


def months_needed():
    ms = set()
    for p in PERIODS.values():
        a = pd.Timestamp(p["start"]) - pd.Timedelta(days=LOOKBACK_D)
        b = pd.Timestamp(p["end"])
        for m in pd.period_range(a, b, freq="M"):
            ms.add(m)
    return sorted(ms)


def _kl_frame(parts):
    k = pd.concat(parts)
    k.columns = ["ts", "o", "h", "l", "c", "v", "qv", "tqv"]
    k["ts"] = _ms(k["ts"])
    k = k.dropna().astype("float64").drop_duplicates("ts").sort_values("ts")
    return k


def fetch_symbol_1h(sym):
    path = os.path.join(H1_DIR, f"{sym}.npz")
    if os.path.exists(path):
        return "cached"
    end = archive_end()
    cur = pd.Period(end, freq="M")
    parts, fr = [], []
    for m in months_needed():
        if m < cur:
            b = _get(f"{BASE}/data/futures/um/monthly/klines/{sym}/1h/{sym}-1h-{m.strftime('%Y-%m')}.zip")
            if b:
                parts.append(_read_zip(b, [0, 1, 2, 3, 4, 5, 7, 10]))
            f = _get(f"{BASE}/data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{m.strftime('%Y-%m')}.zip")
            if f:
                x = _read_zip(f, [0, 2])
                fr.append(pd.DataFrame({"ts": _ms(x.iloc[:, 0]), "r": pd.to_numeric(x.iloc[:, 1], errors="coerce")}))
        elif m == cur:
            d = date(end.year, end.month, 1)
            while d <= end:
                b = _get(f"{BASE}/data/futures/um/daily/klines/{sym}/1h/{sym}-1h-{d:%Y-%m-%d}.zip")
                if b:
                    parts.append(_read_zip(b, [0, 1, 2, 3, 4, 5, 7, 10]))
                d += timedelta(days=1)
    if not parts:
        np.savez_compressed(path, k=np.empty((0, 8)), f=np.empty((0, 2)))
        return "nodata"
    k = _kl_frame(parts)
    f = pd.concat(fr).dropna().drop_duplicates("ts").sort_values("ts").to_numpy() if fr else np.empty((0, 2))
    np.savez_compressed(path, k=k.to_numpy(), f=f)
    return f"ok {len(k)}"


def cmd_fetch(a):
    os.makedirs(H1_DIR, exist_ok=True)
    print("archive end:", archive_end(), flush=True)
    syms = list_symbols()
    if a.limit:
        syms = ["BTCUSDT"] + [s for s in syms if s != "BTCUSDT"][: a.limit]
    elif "BTCUSDT" not in syms:
        syms = ["BTCUSDT"] + syms
    print(len(syms), "symbols,", len(months_needed()), "months", flush=True)
    cnt = {}
    with ThreadPoolExecutor(a.workers) as ex:
        for i, (s, st) in enumerate(zip(syms, ex.map(fetch_symbol_1h, syms))):
            k = st.split()[0]
            cnt[k] = cnt.get(k, 0) + 1
            if i % 50 == 0:
                print(i, s, st, cnt, flush=True)
    print("fetch done", cnt, flush=True)


def load_1h(sym):
    z = np.load(os.path.join(H1_DIR, f"{sym}.npz"))
    k = pd.DataFrame(z["k"], columns=["ts", "o", "h", "l", "c", "v", "qv", "tqv"])
    k.index = pd.to_datetime(k.pop("ts").astype("int64"), unit="ms")
    f = pd.Series(z["f"][:, 1], index=pd.to_datetime(z["f"][:, 0].astype("int64"), unit="ms")) if len(z["f"]) else pd.Series(dtype=float)
    return k, f


def m15_day(sym, d):
    """15m 하루치 (캐시: 원본 zip)."""
    os.makedirs(os.path.join(M15_DIR, sym), exist_ok=True)
    path = os.path.join(M15_DIR, sym, f"{d:%Y-%m-%d}.zip")
    if not os.path.exists(path):
        b = _get(f"{BASE}/data/futures/um/daily/klines/{sym}/15m/{sym}-15m-{d:%Y-%m-%d}.zip")
        with open(path, "wb") as fh:
            fh.write(b or b"")
    b = open(path, "rb").read()
    return _read_zip(b, [0, 1, 2, 3, 4, 5, 7, 10]) if b else None


def load_15m(sym, days):
    parts = [x for x in (m15_day(sym, d) for d in days) if x is not None]
    if not parts:
        return None
    k = _kl_frame(parts)
    k.index = pd.to_datetime(k.pop("ts").astype("int64"), unit="ms")
    return k[k.qv > 0]


# ------------------------------------------------------------------ 지표
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def btc_filter():
    k, _ = load_1h("BTCUSDT")
    d = k.c.resample("1D").last().dropna()
    ok = (d > d.rolling(20).mean())
    ok.index = ok.index + pd.Timedelta(days=1)   # 하루가 끝난 뒤에 알 수 있음
    return ok


def setups_for(sym, k, f, p_start, p_end, btc_ok):
    """4h 셋업 시각(봉 마감) 목록과 4h/1h 프레임."""
    k4 = k.resample("4h").agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum", "qv": "sum"}).dropna()
    t_close = k4.index + pd.Timedelta(hours=4)
    c, h, l, v = k4.c, k4.h, k4.l, k4.v
    ret7 = c / c.shift(42) - 1
    low14 = l.rolling(84).min()
    bleed = (ret7 <= -0.15) & (c <= low14 * 1.08)
    v8 = v.rolling(2).sum()
    vol_q = (v8 >= 3 * v8.rolling(42).mean().shift(2)) & ((c / c.shift(2) - 1).abs() <= 0.03)
    bbw = c.rolling(20).std() / c.rolling(20).mean()
    bb_q = bbw.rolling(180, min_periods=90).rank(pct=True) <= 0.10
    if len(f):
        fr = f.rolling("24h").mean().reindex(t_close, method="ffill").values
        fund_q = pd.Series(fr <= -0.0003, index=k4.index)
    else:
        fund_q = pd.Series(False, index=k4.index)
    pre = vol_q | bb_q | fund_q
    liq = c.mul(0).add(k.qv.rolling(24).sum().reindex(t_close - pd.Timedelta(hours=1)).values) >= 2e6
    age = pd.Series((t_close - k.index[0]).days >= 7, index=k4.index)
    zero = (k.qv <= 0).astype(int).rolling(720, min_periods=1).sum()
    clean = pd.Series(zero.reindex(t_close - pd.Timedelta(hours=1)).values == 0, index=k4.index)
    base = bleed & liq & age & clean
    inwin = (t_close >= pd.Timestamp(p_start)) & (t_close <= pd.Timestamp(p_end) + pd.Timedelta(days=1))
    btc = btc_ok.reindex(t_close, method="ffill").fillna(False).values
    df = pd.DataFrame({"t": t_close, "A": (base & pre).values, "B": base.values, "btc": btc})[inwin]
    return df[df.B], k4


# ------------------------------------------------------------------ 시뮬레이션
def simulate(bars, i0, entry, sl, mode, max_end):
    """bars: o,h,l,c DataFrame (index=open time). i0: 진입 후 첫 봉 위치(진입은 그 봉 시가 또는 직전 종가)."""
    R = entry - sl
    tp = entry + 3 * R
    stop, half, peak, realized = sl, False, entry, 0.0
    o, hh, ll, cc, idx = bars.o.values, bars.h.values, bars.l.values, bars.c.values, bars.index
    i = i0
    while i < len(bars):
        if idx[i] >= max_end:
            px = o[i]
            return (realized + (0.5 if half else 1.0) * (px / entry - 1)), idx[i], "time"
        if ll[i] <= stop:
            px = min(stop, o[i])
            rem = 0.5 if half else 1.0
            return realized + rem * (px / entry - 1), idx[i], ("sl" if stop < entry else "trail")
        if mode == "X1":
            if hh[i] >= tp:
                return tp / entry - 1, idx[i], "tp"
        else:
            if not half and hh[i] >= entry * 1.15:
                half, realized = True, 0.5 * 0.15
            peak = max(peak, hh[i])
            new = stop
            if peak >= entry + R:
                new = max(new, entry)
            if half:
                new = max(new, peak * 0.88)
            stop = new   # 다음 봉부터 적용
        i += 1
    px = cc[-1]
    return (realized + (0.5 if half else 1.0) * (px / entry - 1)), idx[-1], "eod"


def clamp_sl(entry, raw):
    d = (entry - raw) / entry
    d = min(max(d, 0.03), 0.08)
    return entry * (1 - d)


def entry_4h(k4, k1h, t_setup):
    """기존 4h 돌파 진입. 반환 (진입시각, 진입가, 손절, 청산용 1h bars, 시작 idx)."""
    hh6 = k4.h.shift(1).rolling(6).max()
    va = k4.v.shift(1).rolling(20).mean()
    ll6 = k4.l.rolling(6).min()
    win = k4[(k4.index + pd.Timedelta(hours=4) > t_setup) & (k4.index + pd.Timedelta(hours=4) <= t_setup + pd.Timedelta(hours=24))]
    for t0 in win.index:
        if k4.c[t0] > hh6[t0] and k4.v[t0] >= 1.5 * va[t0]:
            te = t0 + pd.Timedelta(hours=4)
            e = k4.c[t0]
            bars = k1h[k1h.index >= te]
            return te, e, clamp_sl(e, ll6[t0]), bars, 0
    return None


def entry_mtf(sym, k1h, t_setup):
    """1h EMA20 확인 → 15m 거래량 돌파 진입."""
    c1 = k1h.c
    e20 = ema(c1, 20)
    w1 = k1h[(k1h.index + pd.Timedelta(hours=1) > t_setup) & (k1h.index + pd.Timedelta(hours=1) <= t_setup + pd.Timedelta(hours=24))]
    conf = [t for t in w1.index if c1[t] > e20[t]]
    if not conf:
        return None
    t_conf = conf[0] + pd.Timedelta(hours=1)
    d0 = (t_setup - pd.Timedelta(days=1)).date()
    days = [d0 + timedelta(days=i) for i in range(6)]
    days = [d for d in days if d <= archive_end()]
    m = load_15m(sym, days)
    if m is None or len(m) < 120:
        return None
    hh24 = m.h.shift(1).rolling(24).max()
    ll24 = m.l.shift(1).rolling(24).min()
    va = m.v.shift(1).rolling(96).mean()
    low96 = m.l.rolling(96).min()
    tc = m.index + pd.Timedelta(minutes=15)
    cand = (tc > t_conf) & (tc <= t_setup + pd.Timedelta(hours=24))
    sig = cand & (m.c > hh24).values & (m.v >= 3 * va).values & (m.c <= low96 * 1.15).values
    pos = np.flatnonzero(sig)
    if not len(pos) or pos[0] + 1 >= len(m):
        return None
    i = pos[0]
    e = m.o.iloc[i + 1]
    return m.index[i + 1], e, clamp_sl(e, min(ll24.iloc[i], m.l.iloc[i])), m, i + 1


# ------------------------------------------------------------------ 실행
def portfolio(tr, alloc=0.10, max_pos=10):
    if tr.empty:
        return 0.0, 0.0, 0
    tr = tr.sort_values("entry_t")
    eq, peak, mdd, open_, taken = 1.0, 1.0, 0.0, [], 0
    for _, r in tr.iterrows():
        # 진입 전에 끝난 포지션 정산
        for p in sorted([p for p in open_ if p[0] <= r.entry_t], key=lambda x: x[0]):
            eq += p[1] * p[2]; open_.remove(p)
            peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
        if len(open_) >= max_pos or any(p[3] == r.symbol for p in open_):
            continue
        open_.append((r.exit_t, eq * alloc, r.net, r.symbol)); taken += 1
    for p in sorted(open_, key=lambda x: x[0]):
        eq += p[1] * p[2]; peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    return eq - 1, mdd, taken


def stats(tr):
    if tr.empty:
        return dict(n=0)
    w, lo = tr.net[tr.net > 0].sum(), -tr.net[tr.net <= 0].sum()
    ret, mdd, taken = portfolio(tr)
    return dict(n=len(tr), syms=int(tr.symbol.nunique()), win=round((tr.net > 0).mean(), 3),
                avg=round(tr.net.mean() * 100, 2), med=round(tr.net.median() * 100, 2),
                PF=round(w / lo, 2) if lo > 0 else None, sum=round(tr.net.sum() * 100, 1),
                port_ret=round(ret * 100, 1), port_mdd=round(mdd * 100, 1), port_trades=taken,
                exits={k: int(v) for k, v in tr.reason.value_counts().items()})


def run_period(key, p, syms, btc_ok, fee, workers):
    p_start, p_end = p["start"], p["end"]
    max_end_all = pd.Timestamp(p_end) + pd.Timedelta(days=1)

    def one(sym):
        try:
            k, f = load_1h(sym)
        except Exception:
            return []
        if len(k) < 24 * 40:
            return []
        k = k[(k.index >= pd.Timestamp(p_start) - pd.Timedelta(days=LOOKBACK_D)) & (k.index < max_end_all + pd.Timedelta(days=4))]
        if len(k) < 24 * 40:
            return []
        st, k4 = setups_for(sym, k, f, p_start, p_end, btc_ok)
        out, busy_until = [], {}
        for _, s in st.iterrows():
            for mode in ("4h", "mtf"):
                if busy_until.get(mode, pd.Timestamp(0)) > s.t:
                    continue
                r = entry_4h(k4, k, s.t) if mode == "4h" else entry_mtf(sym, k, s.t)
                if r is None:
                    continue
                te, e, sl, bars, i0 = r
                if te > max_end_all:
                    continue
                ends = []
                for ex in ("X1", "X2"):
                    g, xt, why = simulate(bars, i0, e, sl, ex, te + pd.Timedelta(hours=72))
                    out.append(dict(period=key, symbol=sym, mode=mode, exit=ex, setup_t=s.t, A=bool(s.A),
                                    btc=bool(s.btc), entry_t=te, exit_t=xt, entry=e, sl=sl,
                                    gross=g, net=g - fee, reason=why))
                    ends.append(xt)
                busy_until[mode] = max(ends)
        return out

    rows = []
    with ThreadPoolExecutor(workers) as ex:
        for i, r in enumerate(ex.map(one, syms)):
            rows += r
            if i % 100 == 0:
                print(f"  [{key}] {i}/{len(syms)} trades so far {len(rows)}", flush=True)
    return pd.DataFrame(rows)


def btc_bh(p):
    k, _ = load_1h("BTCUSDT")
    s = k.c[(k.index >= pd.Timestamp(p["start"])) & (k.index < pd.Timestamp(p["end"]) + pd.Timedelta(days=1))]
    return round((s.iloc[-1] / s.iloc[0] - 1) * 100, 1)


def cmd_run(a):
    os.makedirs(OUT, exist_ok=True)
    syms = sorted(f[:-4] for f in os.listdir(H1_DIR) if f.endswith(".npz"))
    if a.limit:
        syms = syms[: a.limit + 1]
    btc_ok = btc_filter()
    keys = a.periods.split(",") if a.periods else list(PERIODS)
    summary, all_tr = {}, []
    for key in keys:
        p = PERIODS[key]
        print(f"== {key} {p['name']} {p['start']}~{p['end']}", flush=True)
        tr = run_period(key, p, syms, btc_ok, a.fee, a.workers)
        all_tr.append(tr)
        res = {"name": p["name"], "start": p["start"], "end": p["end"], "btc_buy_hold": btc_bh(p), "variants": {}}
        if not tr.empty:
            for mode in ("4h", "mtf"):
                for ex in ("X1", "X2"):
                    for setup in ("A", "B"):
                        for bf in (False, True):
                            t = tr[(tr["mode"] == mode) & (tr["exit"] == ex)]
                            if setup == "A":
                                t = t[t.A]
                            if bf:
                                t = t[t.btc]
                            res["variants"][f"{mode}|{ex}|{setup}{'|BTC>SMA20' if bf else ''}"] = stats(t)
        summary[key] = res
    tr = pd.concat(all_tr) if all_tr else pd.DataFrame()
    tr.to_csv(os.path.join(OUT, "trades.csv"), index=False)
    json.dump(summary, open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    write_md(summary, a.fee)
    print("saved", OUT)


def write_md(summary, fee):
    L = ["# 국면별 백테스트 결과", "",
         f"수수료 왕복 {fee*100:.2f}% 반영, 롱만. 셋업 A = bleed + 선행신호, B = bleed 만. "
         "4h = 기존 4h 돌파 진입, mtf = 4h 셋업 → 1h EMA20 확인 → 15m 거래량 돌파 진입. "
         "X1 = 손절/3R 익절/72h, X2 = 본전 이동 + 15% 절반 익절 + 12% 트레일링/72h. "
         "포트폴리오 = 자산 10%씩 최대 10개 동시.", ""]
    for key, r in summary.items():
        L += [f"## {key} — {r['name']} ({r['start']} ~ {r['end']}), BTC 보유 {r['btc_buy_hold']:+.1f}%", "",
              "| 변형 | 거래 | 승률 | 평균% | 중앙% | PF | 합계% | 포트폴리오% | MDD% |", "|---|---|---|---|---|---|---|---|---|"]
        for v, s in r["variants"].items():
            if not s.get("n"):
                L.append(f"| {v} | 0 | | | | | | | |"); continue
            L.append(f"| {v} | {s['n']} | {s['win']:.0%} | {s['avg']:+.2f} | {s['med']:+.2f} | {s['PF']} | {s['sum']:+.1f} | {s['port_ret']:+.1f} | {s['port_mdd']:.1f} |")
        L.append("")
    open(os.path.join(OUT, "summary.md"), "w", encoding="utf-8").write("\n".join(L))
    print("\n".join(L))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "run", "all"])
    ap.add_argument("--fee", type=float, default=0.002)
    ap.add_argument("--periods", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    if a.cmd in ("fetch", "all"):
        cmd_fetch(a)
    if a.cmd in ("run", "all"):
        cmd_run(a)
