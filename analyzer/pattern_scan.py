"""급등 직전 차트 패턴 기록 + 유사 종목 선정 — 바이낸스 USDT-M 무기한 선물.

흐름
  1) 현재 상승률 상위 N개(기본 5) 종목 선정 (24h 상승률, 주식/ETF/원자재 선물 제외)
  2) 각 종목의 급등 시작점(launch) 탐지: 최근 --lookback 시간(기본 72h) 안의 최고가(peak),
     그 이전 저점(low). launch = peak 전에 종가가 low + 25%×(peak-low) 이하였던 마지막 봉의 다음 봉
     (= 마지막 상승 구간의 시작). 분석은 launch 봉 "이전에 마감된 봉"까지만 사용 (상승 직전까지).
       4h  : launch 직전 42봉 (7일)      — 큰 흐름
       1h  : launch 직전 48봉 (2일)      — 중기 흐름
       15m : launch 직전 48봉 (12시간)   — 직전 움직임
     + 특징값: 7일/24h 수익률, 30일 고점 대비, RSI, ATR%, BB폭 순위, 거래량 배수, 거래대금, 펀딩, OI 24h 변화
  3) 패턴 기록: analyzer/patterns/library.jsonl 에 누적 + 종목별 차트 PNG
  4) 나머지 코인의 "지금" 차트(같은 길이 창, 지금 시점에서 끝남)를 각 패턴과 비교
       모양 유사도 = 타임프레임별 정규화 가격 경로 상관(0.7) + 거래량 경로 상관(0.3), 4h/1h/15m 평균
       특징 유사도 = 특징값 차이(전 종목 표준편차로 나눔)의 거리 → exp(-d)
       점수 = 0.5 * 모양 + 0.5 * 특징 (0~1), 패턴별 최고 점수로 순위
     제외: 이미 24h +15% 이상 오른 종목, 24h 거래대금 < $2M, 상장 7일 미만
  5) 결과: analyzer/out/scan_YYYYmmdd_HHMM.md / .csv / 후보 차트 PNG

  방식 2 — 횡보 후 거래량 증가 (--mode range, 기본은 두 방식 모두)
    기준 구간 = 최근 --recent 시간(기본 4h)을 뺀 그 이전 --range-hours 시간(기본 24h, 1h 봉)
    횡보 = 기준 구간 (최고가 - 최저가) / 최저가 <= 3%  또는  <= 5%  (두 단계로 따로 표시)
    거래량 증가 = 최근 4h 평균 거래량 / 기준 구간 평균 거래량 >= --vol-mult (기본 2배)
    표시: 최근 4h 가격 변화, 박스 상단 돌파 여부, 7일 평균 대비 거래량

사용
  python analyzer/pattern_scan.py                  # 바이낸스 API (PC 에서 실행)
  python analyzer/pattern_scan.py --top 5 --pick 20 --library   # 누적 패턴 전체와 비교
  python analyzer/pattern_scan.py --mode range --range-hours 48 --vol-mult 1.5   # 횡보 방식만
  python analyzer/pattern_scan.py --source archive # API 차단 환경 테스트용 (data.binance.vision, 1~2일 지연)

API 키 불필요 (공개 시세만 사용). 요청 가중치를 보고 분당 한도 아래로 자동 조절.
"""
from __future__ import annotations

import argparse, json, math, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "study"))
sys.path.insert(0, os.path.join(ROOT, "backtest"))
from tradfi import is_tradfi  # noqa: E402

PAT_DIR, OUT_DIR = os.path.join(HERE, "patterns"), os.path.join(HERE, "out")
FAPI = "https://fapi.binance.com"
WIN = {"4h": 42, "1h": 48, "15m": 48}          # 분석 창 (봉 수)
NEED = {"4h": 220, "1h": 499, "15m": 200}       # 특징 계산용으로 받을 봉 수
MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
FEATS = ["ret_7d", "ret_24h", "off_high_30d", "rsi_1h", "atr_pct_1h", "bbw_rank_4h",
         "vol_ratio_8h", "log_qv24", "funding", "oi_chg_24h"]
NOT_COIN = {"USDBRLUSDT", "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "EURUSDT", "GBPUSDT", "AEURUSDT", "USDPUSDT"}
FEAT_KO = {"ret_7d": "7일 수익률", "ret_24h": "24h 수익률", "off_high_30d": "30일 고점 대비",
           "rsi_1h": "1h RSI", "atr_pct_1h": "1h ATR%", "bbw_rank_4h": "4h BB폭 30일 순위",
           "vol_ratio_8h": "8h 거래량/7일 평균", "log_qv24": "log 24h 거래대금", "funding": "펀딩",
           "oi_chg_24h": "OI 24h 변화"}


# ------------------------------------------------------------------ 데이터 소스
class BinanceAPI:
    """공개 REST. 가중치 헤더를 보고 1분 한도(2400)의 75% 넘으면 대기."""

    def __init__(self):
        self.s = requests.Session()
        self.now_ms = None

    def _get(self, path, **params):
        for a in range(6):
            try:
                r = self.s.get(FAPI + path, params=params, timeout=20)
            except requests.RequestException:
                time.sleep(2 * (a + 1)); continue
            used = int(r.headers.get("X-MBX-USED-WEIGHT-1M", 0) or 0)
            if used > 1800:
                time.sleep(60 - datetime.now().second + 1)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (418, 429):
                time.sleep(int(r.headers.get("Retry-After", 30))); continue
            if r.status_code == 451:
                raise SystemExit("바이낸스 API 가 이 지역에서 차단됨 (451). PC 에서 실행하거나 --source archive 사용.")
            if r.status_code == 400:
                return None
            time.sleep(2 * (a + 1))
        return None

    def now(self):
        if self.now_ms is None:
            self.now_ms = self._get("/fapi/v1/time")["serverTime"]
        return self.now_ms

    def universe(self):
        info = self._get("/fapi/v1/exchangeInfo")
        syms = [s["symbol"] for s in info["symbols"] if s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"]
        tick = {t["symbol"]: t for t in self._get("/fapi/v1/ticker/24hr")}
        rows = [dict(symbol=s, chg24=float(tick[s]["priceChangePercent"]) / 100, qv24=float(tick[s]["quoteVolume"]))
                for s in syms if s in tick and not is_tradfi(s)]
        return pd.DataFrame(rows)

    def klines(self, sym, iv, limit, end_ms=None):
        p = dict(symbol=sym, interval=iv, limit=limit)
        if end_ms:
            p["endTime"] = int(end_ms) - 1
        d = self._get("/fapi/v1/klines", **p)
        if not d:
            return None
        k = pd.DataFrame([[float(x[0]), *map(float, x[1:6]), float(x[7]), float(x[6])] for x in d],
                         columns=["ts", "o", "h", "l", "c", "v", "qv", "ct"])
        k = k[k.ct < (end_ms or self.now())]        # 마감된 봉만
        k.index = pd.to_datetime(k.pop("ts").astype("int64"), unit="ms")
        return k.drop(columns="ct")

    def funding(self, sym, end_ms=None):
        p = dict(symbol=sym, limit=10)
        if end_ms:
            p["endTime"] = int(end_ms)
        d = self._get("/fapi/v1/fundingRate", **p)
        return float(d[-1]["fundingRate"]) if d else np.nan

    def oi_chg(self, sym, end_ms=None):
        """OI 24h 변화. 바이낸스는 최근 30일만 제공."""
        p = dict(symbol=sym, period="1h", limit=25)
        if end_ms:
            p["endTime"] = int(end_ms)
        d = self._get("/futures/data/openInterestHist", **p)
        if not d or len(d) < 25:
            return np.nan
        a, b = float(d[-1]["sumOpenInterest"]), float(d[0]["sumOpenInterest"])
        return a / b - 1 if b > 0 else np.nan


class Archive:
    """data.binance.vision 일별 아카이브 (API 차단 환경 테스트용). '지금' = 아카이브 마지막 날 다음 0시."""

    def __init__(self):
        import regime_bt as rb
        self.rb = rb
        self.end = pd.Timestamp(rb.archive_end()) + pd.Timedelta(days=1)
        self.cache = {}

    def now(self):
        return int(self.end.value // 1_000_000)

    def _h1(self, sym):
        if sym not in self.cache:
            path = os.path.join(self.rb.H1_DIR, f"{sym}.npz")
            if os.path.exists(path):
                k, f = self.rb.load_1h(sym)
            else:
                days = pd.date_range(self.end - pd.Timedelta(days=40), self.end - pd.Timedelta(days=1))
                parts = []
                for d in days:
                    b = self.rb._get(f"{self.rb.BASE}/data/futures/um/daily/klines/{sym}/1h/{sym}-1h-{d:%Y-%m-%d}.zip")
                    if b:
                        parts.append(self.rb._read_zip(b, [0, 1, 2, 3, 4, 5, 7, 10]))
                if not parts:
                    self.cache[sym] = (None, pd.Series(dtype=float)); return self.cache[sym]
                k = self.rb._kl_frame(parts)
                k.index = pd.to_datetime(k.pop("ts").astype("int64"), unit="ms")
                f = pd.Series(dtype=float)
            self.cache[sym] = (k[k.qv > 0].drop(columns="tqv"), f)
        return self.cache[sym]

    def universe(self):
        rows = []
        for s in self.rb.list_symbols():
            k, _ = self._h1(s)
            if k is None or len(k) < 30 or k.index[-1] < self.end - pd.Timedelta(hours=1):
                continue
            rows.append(dict(symbol=s, chg24=k.c.iloc[-1] / k.c.iloc[-25] - 1, qv24=k.qv.iloc[-24:].sum()))
        return pd.DataFrame(rows)

    def klines(self, sym, iv, limit, end_ms=None):
        end = pd.Timestamp(end_ms or self.now(), unit="ms")
        if iv == "15m":
            days = pd.date_range((end - pd.Timedelta(minutes=15 * limit)).normalize(), end.normalize())
            k = self.rb.load_15m(sym, days)
            if k is None:
                return None
            k = k.drop(columns="tqv")
        else:
            k, _ = self._h1(sym)
            if k is None:
                return None
            if iv == "4h":
                k = k.resample("4h").agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum", "qv": "sum"}).dropna()
        k = k[k.index + pd.Timedelta(milliseconds=MS[iv]) <= end]
        return k.iloc[-limit:]

    def funding(self, sym, end_ms=None):
        _, f = self._h1(sym)
        f = f[f.index <= pd.Timestamp(end_ms or self.now(), unit="ms")]
        return float(f.iloc[-1]) if len(f) else np.nan

    def oi_chg(self, sym, end_ms=None):
        t = pd.Timestamp(end_ms or self.now(), unit="ms") - pd.Timedelta(minutes=5)
        return float(self.rb.oi_change_24h(sym, [t]).iloc[0])


# ------------------------------------------------------------------ 지표 / 패턴
def rsi(c, n=14):
    d = c.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / n).mean(), (-d.clip(upper=0)).ewm(alpha=1 / n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def features(k1, k4):
    c1, c4 = k1.c, k4.c
    tr = pd.concat([k1.h - k1.l, (k1.h - c1.shift()).abs(), (k1.l - c1.shift()).abs()], axis=1).max(axis=1)
    bbw = c4.rolling(20).std() / c4.rolling(20).mean()
    v = k1.v
    return dict(
        ret_7d=c1.iloc[-1] / c1.iloc[-169] - 1 if len(c1) > 169 else np.nan,
        ret_24h=c1.iloc[-1] / c1.iloc[-25] - 1,
        off_high_30d=c1.iloc[-1] / k4.h.iloc[-180:].max() - 1,
        rsi_1h=float(rsi(c1).iloc[-1]),
        atr_pct_1h=float(tr.rolling(14).mean().iloc[-1] / c1.iloc[-1]),
        bbw_rank_4h=float((bbw.iloc[-180:] <= bbw.iloc[-1]).mean()),
        vol_ratio_8h=float(v.iloc[-8:].mean() / v.iloc[-176:-8].mean()) if len(v) > 176 else np.nan,
        log_qv24=float(np.log10(max(k1.qv.iloc[-24:].sum(), 1))),
    )


def shape(k, n):
    """정규화 가격 경로(로그 종가 z-score) 와 거래량 경로(log 거래량 z-score)."""
    k = k.iloc[-n:]
    p = np.log(k.c.values)
    v = np.log(np.maximum(k.v.values, 1e-12))
    z = lambda x: ((x - x.mean()) / x.std()).tolist() if x.std() > 0 else [0.0] * len(x)
    return dict(price=z(p), vol=z(v))


def snapshot(src, sym, end_ms=None):
    """end_ms 직전에 마감된 봉까지의 3개 타임프레임 + 특징. 데이터 부족하면 None."""
    ks = {iv: src.klines(sym, iv, NEED[iv], end_ms) for iv in WIN}
    if any(ks[iv] is None or len(ks[iv]) < WIN[iv] for iv in WIN) or len(ks["1h"]) < 200 or len(ks["4h"]) < 60:
        return None
    f = features(ks["1h"], ks["4h"])
    f["funding"] = src.funding(sym, end_ms)
    f["oi_chg_24h"] = src.oi_chg(sym, end_ms)
    return dict(symbol=sym, t=str(pd.Timestamp(end_ms or src.now(), unit="ms")), feat=f,
                shape={iv: shape(ks[iv], WIN[iv]) for iv in WIN}, _k=ks)


def find_launch(k1, lookback, frac=0.25):
    """최근 lookback 시간 안의 최고가 봉(peak), 그 이전 최저가(low).
    launch = peak 이전에 종가가 low + frac×(peak-low) 이하였던 마지막 봉의 다음 봉 = 마지막 상승 구간의 시작.
    반환 (launch 봉 시각, peak 시각, launch 직전 종가 대비 peak 상승률, low 대비 peak 상승률)."""
    w = k1.iloc[-lookback:]
    ip = int(np.argmax(w.h.values))
    lo = w.l.values[: ip + 1].min()
    pk = w.h.values[ip]
    below = np.where(w.c.values[:ip] <= lo + frac * (pk - lo))[0]
    il = int(below[-1]) + 1 if len(below) else 0
    base = w.c.values[il - 1] if il > 0 else w.o.values[0]
    return w.index[il], w.index[ip], pk / base - 1, pk / lo - 1


def corr(a, b):
    a, b = np.asarray(a), np.asarray(b)
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def similarity(pat, cur, fscale):
    shp = np.mean([0.7 * corr(pat["shape"][iv]["price"], cur["shape"][iv]["price"])
                   + 0.3 * corr(pat["shape"][iv]["vol"], cur["shape"][iv]["vol"]) for iv in WIN])
    shp = (shp + 1) / 2
    d, n = 0.0, 0
    for f in FEATS:
        a, b = pat["feat"].get(f), cur["feat"].get(f)
        if a is None or b is None or not np.isfinite(a) or not np.isfinite(b) or not fscale.get(f):
            continue
        d += ((a - b) / fscale[f]) ** 2; n += 1
    fs = math.exp(-math.sqrt(d / n)) if n else 0.0
    per = {iv: round(corr(pat["shape"][iv]["price"], cur["shape"][iv]["price"]), 2) for iv in WIN}
    return 0.5 * shp + 0.5 * fs, shp, fs, per


def range_scan(k1s, hours, recent, vol_mult, max_chg):
    """방식 2: 횡보(3%/5%) 종목 중 최근 거래량이 늘어난 종목."""
    rows = []
    for sym, k in k1s.items():
        if k is None or len(k) < hours + recent + 1:
            continue
        base, rec = k.iloc[-(hours + recent):-recent], k.iloc[-recent:]
        lo, hi = base.l.min(), base.h.max()
        rng = hi / lo - 1
        if rng > 0.05 or base.v.mean() <= 0:
            continue
        vr = rec.v.mean() / base.v.mean()
        v7 = k.v.iloc[-(168 + recent):-recent].mean() if len(k) > 168 + recent else np.nan
        mv = rec.c.iloc[-1] / base.c.iloc[-1] - 1
        rows.append(dict(symbol=sym, box="3%" if rng <= 0.03 else "5%", range_pct=round(rng, 4),
                         vol_ratio=round(vr, 2), vol_vs_7d=round(rec.v.mean() / v7, 2) if v7 and np.isfinite(v7) else None,
                         move_recent=round(mv, 4), breakout=bool(rec.c.iloc[-1] > hi), below=bool(rec.c.iloc[-1] < lo),
                         qv24=round(k.qv.iloc[-24:].sum())))
    df = pd.DataFrame(rows)
    if df.empty:
        return df, df
    sel = df[(df.vol_ratio >= vol_mult) & (df.move_recent.abs() < max_chg)].sort_values(["box", "vol_ratio"], ascending=[True, False])
    return df, sel


# ------------------------------------------------------------------ 차트
def plot_pattern(snap, path, title, launch=None, after=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(3, 1, figsize=(11, 9))
    for a, iv in zip(ax, ("4h", "1h", "15m")):
        k = snap["_k"][iv].iloc[-WIN[iv]:]
        if after is not None and iv in after and after[iv] is not None:
            k = pd.concat([k, after[iv]])
        x = np.arange(len(k))
        up = k.c >= k.o
        a.vlines(x, k.l, k.h, color=np.where(up, "#1a9850", "#d73027"), lw=0.8)
        a.bar(x, (k.c - k.o).abs().clip(lower=k.c * 1e-4), bottom=np.minimum(k.o, k.c), width=0.7,
              color=np.where(up, "#1a9850", "#d73027"))
        a2 = a.twinx(); a2.bar(x, k.v, width=0.7, color="#999", alpha=0.25); a2.set_yticks([])
        a.axvline(WIN[iv] - 0.5, color="#333", ls="--", lw=1)
        step = max(1, len(k) // 8)
        a.set_xticks(x[::step]); a.set_xticklabels([t.strftime("%m-%d %H:%M") for t in k.index[::step]], fontsize=7)
        a.set_title(f"{iv}  (dashed = {'launch' if launch else 'now'})", fontsize=9)
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


# ------------------------------------------------------------------ 메인
def main(a):
    try:
        import matplotlib
        from matplotlib import font_manager
        have = {f.name for f in font_manager.fontManager.ttflist}
        ko = [f for f in ("Malgun Gothic", "AppleGothic", "NanumGothic", "Noto Sans CJK KR") if f in have]
        matplotlib.rcParams["font.family"] = ko + ["DejaVu Sans"]
        matplotlib.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass
    os.makedirs(PAT_DIR, exist_ok=True); os.makedirs(OUT_DIR, exist_ok=True)
    src = BinanceAPI() if a.source == "api" else Archive()
    now = pd.Timestamp(src.now(), unit="ms")
    stamp = base = now.strftime("%Y%m%d_%H%M")
    i = 1
    while os.path.exists(os.path.join(OUT_DIR, f"run_{stamp}.json")):   # 같은 분에 여러 번 실행해도 덮어쓰지 않음
        i += 1; stamp = f"{base}_{i}"
    print(f"기준 시각(UTC) {now}  소스 {a.source}", flush=True)

    uni = src.universe()
    uni = uni[(uni.qv24 >= a.min_qv) & ~uni.symbol.isin(NOT_COIN)]
    top = uni.sort_values("chg24", ascending=False).head(a.top)
    print("상승 상위:", ", ".join(f"{r.symbol} {r.chg24:+.1%}" for r in top.itertuples()), flush=True)

    if a.mode == "range":
        k1s = fetch_1h(src, uni.symbol.tolist(), a.workers)
        report = new_report(a, now, stamp, top)
        report["range"] = do_range(a, k1s, now, stamp, uni)
        save_report(report, stamp)
        return

    # 1~3) 상위 종목: launch 직전 패턴 기록
    patterns = []
    for r in top.itertuples():
        k1 = src.klines(r.symbol, "1h", NEED["1h"])
        if k1 is None or len(k1) < a.lookback + 10:
            print(f"  {r.symbol}: 데이터 부족, 건너뜀"); continue
        t_launch, t_peak, gain, gain_low = find_launch(k1, a.lookback, a.launch_frac)
        end_ms = int(t_launch.value // 1_000_000)
        snap = snapshot(src, r.symbol, end_ms)
        if snap is None:
            print(f"  {r.symbol}: launch 이전 데이터 부족, 건너뜀"); continue
        snap.update(kind="pump", launch=str(t_launch), peak=str(t_peak), gain=round(gain, 4), gain_from_low=round(gain_low, 4),
                    chg24=round(r.chg24, 4), recorded=str(now))
        patterns.append(snap)
        after = {iv: src.klines(r.symbol, iv, WIN[iv], min(end_ms + WIN[iv] * MS[iv], src.now())) for iv in WIN}
        after = {iv: (k[k.index >= t_launch] if k is not None else None) for iv, k in after.items()}
        png = os.path.join(PAT_DIR, f"{t_launch:%Y%m%d_%H%M}_{r.symbol}.png")
        plot_pattern(snap, png, f"{r.symbol} pump pattern  launch {t_launch:%m-%d %H:%M} -> peak {t_peak:%m-%d %H:%M}  +{gain:.0%}",
                     launch=True, after=after)
        snap["png"] = os.path.relpath(png, ROOT).replace(os.sep, "/")
        f = snap["feat"]
        print(f"  {r.symbol}: launch {t_launch} peak {t_peak} +{gain:.0%} | 7d {f['ret_7d']:+.1%} "
              f"30d고점대비 {f['off_high_30d']:+.1%} RSI {f['rsi_1h']:.0f} 거래량배수 {f['vol_ratio_8h']:.2f} "
              f"OI24h {f['oi_chg_24h']:+.1%}" if np.isfinite(f['oi_chg_24h']) else f"  {r.symbol}: launch {t_launch} +{gain:.0%}",
              flush=True)

    lib_path = os.path.join(PAT_DIR, "library.jsonl")
    seen = set()
    if os.path.exists(lib_path):
        for line in open(lib_path, encoding="utf-8"):
            p = json.loads(line); seen.add((p["symbol"], p["launch"]))
    with open(lib_path, "a", encoding="utf-8") as fh:
        for p in patterns:
            if (p["symbol"], p["launch"]) not in seen:
                fh.write(json.dumps({k: v for k, v in p.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")
    if a.library and os.path.exists(lib_path):
        cur_keys = {(p["symbol"], p["launch"]) for p in patterns}
        patterns += [p for p in map(json.loads, open(lib_path, encoding="utf-8"))
                     if (p["symbol"], p["launch"]) not in cur_keys]
    if not patterns:
        raise SystemExit("기록된 패턴이 없음")
    print(f"비교할 패턴 {len(patterns)}개", flush=True)

    # 4) 나머지 코인: 지금 시점 차트
    excl = set(top.symbol)
    cand = uni[~uni.symbol.isin(excl) & (uni.chg24 < a.max_chg)].symbol.tolist()
    print(f"후보 {len(cand)}종목 차트 수집…", flush=True)
    with ThreadPoolExecutor(a.workers) as ex:
        snaps = [s for s in ex.map(lambda s: _safe(snapshot, src, s), cand) if s is not None]
    snaps = [s for s in snaps if len(s["_k"]["1h"]) >= 24 * 7]
    fr = pd.DataFrame([s["feat"] for s in snaps])
    fscale = {f: float(fr[f].std()) for f in FEATS if f in fr and fr[f].notna().sum() > 10}

    rows = []
    for s in snaps:
        best = None
        for p in patterns:
            sc, shp, fs, per = similarity(p, s, fscale)
            if best is None or sc > best[0]:
                best = (sc, shp, fs, per, p)
        sc, shp, fs, per, p = best
        rows.append(dict(symbol=s["symbol"], score=round(sc, 3), shape=round(shp, 3), feat=round(fs, 3),
                         like=p["symbol"], like_launch=p["launch"], corr_4h=per["4h"], corr_1h=per["1h"],
                         corr_15m=per["15m"], **{k: round(v, 4) if np.isfinite(v) else None for k, v in s["feat"].items()}))
    res = pd.DataFrame(rows).sort_values("score", ascending=False)
    res.to_csv(os.path.join(OUT_DIR, f"scan_{stamp}.csv"), index=False)

    # 5) 리포트 + 상위 후보 차트
    by = {s["symbol"]: s for s in snaps}
    pick = res.head(a.pick)
    for r in pick.head(a.charts).itertuples():
        plot_pattern(by[r.symbol], os.path.join(OUT_DIR, f"scan_{stamp}_{r.symbol}.png"),
                     f"{r.symbol} now  score {r.score:.2f}  (similar to {r.like} launch {r.like_launch[5:16]})")
    write_md(os.path.join(OUT_DIR, f"scan_{stamp}.md"), now, a, top, patterns, pick, stamp)
    report = new_report(a, now, stamp, top)
    report["patterns"] = [dict(symbol=p["symbol"], chg24=p["chg24"], launch=p["launch"], peak=p["peak"],
                               gain=p["gain"], gain_from_low=p.get("gain_from_low"), png=p.get("png"), feat=_clean(p["feat"]))
                          for p in patterns if p.get("recorded") == str(now)]
    report["library_size"] = sum(1 for _ in open(lib_path, encoding="utf-8")) if os.path.exists(lib_path) else 0
    cands = []
    for i, r in enumerate(pick.to_dict("records")):
        r = _clean(r)
        png = os.path.join(OUT_DIR, f"scan_{stamp}_{r['symbol']}.png")
        r["png"] = os.path.relpath(png, ROOT).replace(os.sep, "/") if os.path.exists(png) else None
        cands.append(r)
    report["candidates"] = cands
    report["n_candidates_scanned"] = len(res)
    print(f"\n유사 차트 상위 {a.pick}:")
    print(pick[["symbol", "score", "shape", "feat", "like", "corr_4h", "corr_1h", "corr_15m",
                "ret_7d", "off_high_30d", "rsi_1h", "vol_ratio_8h", "oi_chg_24h"]].to_string(index=False))
    print(f"\n저장: {os.path.relpath(OUT_DIR, ROOT)}/scan_{stamp}.md (.csv, 차트 PNG)  패턴: {os.path.relpath(lib_path, ROOT)}")

    if a.mode == "both":
        k1s = {s["symbol"]: s["_k"]["1h"] for s in snaps}
        k1s.update(fetch_1h(src, list(excl), a.workers))
        report["range"] = do_range(a, k1s, now, stamp, uni)
    save_report(report, stamp)


def _clean(d):
    out = {}
    for k, v in d.items():
        if isinstance(v, (np.floating, float)):
            v = float(v) if np.isfinite(v) else None
        elif isinstance(v, (np.integer,)):
            v = int(v)
        elif isinstance(v, np.bool_):
            v = bool(v)
        out[k] = v
    return out


def new_report(a, now, stamp, top):
    return dict(stamp=stamp, time=str(now), source=a.source, mode=a.mode,
                params={k: v for k, v in vars(a).items()},
                top=[_clean(r) for r in top[["symbol", "chg24", "qv24"]].to_dict("records")],
                patterns=[], candidates=[], range=None)


def save_report(report, stamp):
    """웹 대시보드(analyzer/web.py)가 읽는 실행 결과."""
    path = os.path.join(OUT_DIR, f"run_{stamp}.json")
    json.dump(report, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(f"저장: {os.path.relpath(path, ROOT)}")


def fetch_1h(src, syms, workers):
    with ThreadPoolExecutor(workers) as ex:
        ks = list(ex.map(lambda s: _safe(src.klines, s, "1h", 200), syms))
    return dict(zip(syms, ks))


def do_range(a, k1s, now, stamp, uni):
    allr, sel = range_scan(k1s, a.range_hours, a.recent, a.vol_mult, a.max_chg)
    print(f"\n[방식 2] 횡보 {a.range_hours}h: 3% 이내 {int((allr.box == '3%').sum()) if len(allr) else 0}종목, "
          f"5% 이내 {len(allr)}종목 → 최근 {a.recent}h 거래량 {a.vol_mult}배 이상 {len(sel)}종목")
    if len(sel):
        print(sel.to_string(index=False))
    allr.to_csv(os.path.join(OUT_DIR, f"range_{stamp}.csv"), index=False)
    L = [f"# 횡보 후 거래량 증가 스캔 {now:%Y-%m-%d %H:%M} UTC", "",
         f"기준 구간 = 최근 {a.recent}h 를 뺀 이전 {a.range_hours}h (1h 봉). 횡보 = (최고-최저)/최저 ≤ 3% 또는 ≤ 5%. "
         f"거래량 증가 = 최근 {a.recent}h 평균 / 기준 구간 평균 ≥ {a.vol_mult}배. 24h 거래대금 ≥ ${a.min_qv/1e6:.0f}M.", "",
         f"횡보 종목: 3% 이내 {int((allr.box == '3%').sum()) if len(allr) else 0}개, 5% 이내(3% 포함) {len(allr)}개", ""]
    for box in ("3%", "5%"):
        t = sel[sel.box == box] if len(sel) else sel
        L += [f"## 횡보 {box} 이내{' (3% 초과)' if box == '5%' else ''} + 거래량 증가 — {len(t)}종목", "",
              "| 종목 | 박스폭 | 거래량 배수(박스 대비) | 7일 평균 대비 | 최근 가격 변화 | 상태 | 24h 거래대금 |", "|---|---|---|---|---|---|---|"]
        for r in t.itertuples():
            st = "박스 상단 돌파" if r.breakout else ("박스 하단 이탈" if r.below else "박스 안")
            L.append(f"| {r.symbol} | {r.range_pct:.1%} | {r.vol_ratio:.1f}x | {r.vol_vs_7d if r.vol_vs_7d is not None else '-'}x | "
                     f"{r.move_recent:+.1%} | {st} | ${r.qv24/1e6:.1f}M |")
        L.append("")
    open(os.path.join(OUT_DIR, f"range_{stamp}.md"), "w", encoding="utf-8").write("\n".join(L))
    print(f"저장: analyzer/out/range_{stamp}.md (.csv)")
    rows = [_clean(r) for r in allr.to_dict("records")] if len(allr) else []
    for r in rows:
        r["selected"] = bool(r["vol_ratio"] >= a.vol_mult and abs(r["move_recent"]) < a.max_chg)
    return dict(hours=a.range_hours, recent=a.recent, vol_mult=a.vol_mult,
                n3=int((allr.box == "3%").sum()) if len(allr) else 0, n5=len(allr), rows=rows)


def _safe(fn, *x):
    try:
        return fn(*x)
    except Exception as e:  # 개별 종목 실패는 건너뜀
        print("  skip", x[-1], type(e).__name__, e, flush=True)
        return None


def _fmt(f, v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "-"
    if f in ("rsi_1h",):
        return f"{v:.0f}"
    if f in ("bbw_rank_4h", "vol_ratio_8h", "log_qv24"):
        return f"{v:.2f}"
    if f == "funding":
        return f"{v*100:+.3f}%"
    return f"{v*100:+.1f}%"


def write_md(path, now, a, top, patterns, pick, stamp):
    L = [f"# 급등 패턴 스캔 {now:%Y-%m-%d %H:%M} UTC", "",
         f"소스 {a.source}, 상위 {a.top}개 패턴(lookback {a.lookback}h) → 후보 유사도 순위. "
         "점수 = 0.5×모양(4h 42봉·1h 48봉·15m 48봉 가격/거래량 경로 상관) + 0.5×특징값 근접도.", "",
         "## 급등 종목의 상승 직전 상태", "",
         "| 종목 | 24h | launch → peak | 상승 | " + " | ".join(FEAT_KO[f] for f in FEATS) + " |",
         "|" + "---|" * (4 + len(FEATS))]
    for p in patterns:
        if p.get("recorded") != str(now):
            continue
        L.append(f"| {p['symbol']} | {p['chg24']:+.1%} | {p['launch'][5:16]} → {p['peak'][5:16]} | +{p['gain']:.0%} | "
                 + " | ".join(_fmt(f, p["feat"].get(f)) for f in FEATS) + " |")
    L += ["", "차트: " + ", ".join(f"`{p['png']}`" for p in patterns if p.get("png") and p.get("recorded") == str(now)), "",
          f"## 지금 비슷한 차트 상위 {a.pick}", "",
          "| 순위 | 종목 | 점수 | 모양 | 특징 | 닮은 패턴 | 상관 4h/1h/15m | 7일 | 30일 고점 대비 | RSI | 거래량배수 | OI 24h | 펀딩 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(pick.itertuples(), 1):
        L.append(f"| {i} | {r.symbol} | {r.score:.2f} | {r.shape:.2f} | {r.feat:.2f} | {r.like} ({r.like_launch[5:10]}) | "
                 f"{r.corr_4h:+.2f}/{r.corr_1h:+.2f}/{r.corr_15m:+.2f} | {_fmt('ret_7d', r.ret_7d)} | "
                 f"{_fmt('off_high_30d', r.off_high_30d)} | {_fmt('rsi_1h', r.rsi_1h)} | {_fmt('vol_ratio_8h', r.vol_ratio_8h)} | "
                 f"{_fmt('oi_chg_24h', r.oi_chg_24h)} | {_fmt('funding', r.funding)} |")
    L += ["", f"상위 {a.charts}개 차트: `analyzer/out/scan_{stamp}_<종목>.png`", "",
          "주의: 유사도는 '급등 직전과 모양이 닮았다'는 뜻일 뿐 급등 확률이 아닙니다. "
          "앞선 연구(study/out/pump_study.md)에서 급등의 다수는 차트상 선행 신호가 약했고, "
          "추격 진입은 평균적으로 손실이었습니다."]
    open(path, "w", encoding="utf-8").write("\n".join(L))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="급등 직전 패턴 기록 + 유사 종목 선정")
    ap.add_argument("--source", choices=["api", "archive"], default="api")
    ap.add_argument("--top", type=int, default=5, help="패턴으로 쓸 상승 상위 종목 수")
    ap.add_argument("--lookback", type=int, default=72, help="launch 탐색 시간(h)")
    ap.add_argument("--launch-frac", type=float, default=0.25,
                    help="launch = peak 전 종가가 저점+이 비율×(고점-저점) 이하였던 마지막 봉 다음")
    ap.add_argument("--pick", type=int, default=20, help="리포트에 올릴 후보 수")
    ap.add_argument("--charts", type=int, default=10, help="차트 PNG 로 저장할 후보 수")
    ap.add_argument("--min-qv", type=float, default=2e6, help="24h 거래대금 하한(USDT)")
    ap.add_argument("--max-chg", type=float, default=0.15, help="이미 이만큼 오른 후보는 제외")
    ap.add_argument("--library", action="store_true", help="이번 패턴 + 누적 패턴 전체와 비교")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--mode", choices=["both", "pattern", "range"], default="both",
                    help="pattern = 급등 패턴 유사도, range = 횡보 후 거래량 증가, both = 둘 다")
    ap.add_argument("--range-hours", type=int, default=24, help="횡보 판정 구간(h)")
    ap.add_argument("--recent", type=int, default=4, help="거래량 증가를 볼 최근 구간(h)")
    ap.add_argument("--vol-mult", type=float, default=2.0, help="거래량 증가 배수 기준")
    main(ap.parse_args())
