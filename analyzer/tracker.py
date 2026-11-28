"""6가지 진입 방식 — 신호 기록 → 이후 성과 추적 → 방식별 비교.

방식 (모두 롱, 24h 거래대금 >= $2M, 이미 24h +15% 이상 오른 종목 제외, 방식별로 스캔당 최대 6개)
  M0 무작위(기준선)     : 조건 없이 무작위 6종목 — 다른 방식이 "그냥 아무거나"보다 나은지 재는 잣대
  M1 급등 차트 유사      : 24h 상승 상위 5개의 상승 직전 패턴과 닮은 상위 6종목 (pattern_scan 유사도)
  M2 횡보 + 거래량 증가  : 직전 24h 박스폭 3%/5% 이내 + 최근 4h 거래량 2배 이상, 박스 하단 이탈 제외
  M3 이평선 골든크로스   : 1h EMA20 이 EMA50 을 최근 4시간 안에 상향 돌파 + 종가 > 1h EMA200
  M4 RSI 과매도 반등     : 1h RSI(14) 가 최근 3봉 안에 30 아래에서 30 위로 회복 (sub=div: 강세 다이버전스 동반)
  M5 펀딩+OI 숏스퀴즈    : 7일 -15% 이하 & 24h -12%~+3% & 펀딩 <= 0 & OI 24h +10% 이상
  M6 BTC 대비 상대강도   : BTC 가 7일 고점 대비 -3% 이하로 눌렸다가 1h EMA20 위로 회복 중일 때,
                           코인/BTC 비율이 7일 고점의 97% 이상 & 7일 수익률이 BTC 보다 10%p 이상 높은 종목
  모든 신호에 btc_ok (BTC 일봉 종가 > 20일선) 를 같이 기록 → 비교 때 BTC 필터 적용/미적용 둘 다 봄.
  같은 방식·같은 종목은 24시간 안에 다시 기록하지 않음.

성과 (신호 시점 종가 기준, 1h 봉으로 계산)
  r4 / r24 / r72 : 4·24·72시간 뒤 수익률,  mfe / mae : 72시간 안 최대 상승 / 최대 하락
  trade : 손절 -8% / 익절 +15% / 72시간 청산, 수수료 0.2% 차감 (같은 봉에서 둘 다 닿으면 손절)
  hit30 : 72시간 안에 +30% 이상 찍었는지

사용
  python analyzer/tracker.py scan                         # 지금 스캔(바이낸스 API) → 신호 기록 + 지난 신호 성과 갱신
  python analyzer/tracker.py backfill --days 60 --every 4 # 과거 60일을 4시간마다 스캔한 것처럼 채움 (공개 아카이브)
  python analyzer/tracker.py update                       # 성과만 갱신
  python analyzer/tracker.py report                       # 방식별 비교표 (analyzer/out/compare.md)
DB: analyzer/data/tracker.db (SQLite). 웹 대시보드의 '6가지 비교' 탭이 이 DB 를 읽음.
"""
from __future__ import annotations

import argparse, json, os, random, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pattern_scan as ps  # noqa: E402

DB = os.path.join(HERE, "data", "tracker.db")
SEED = os.path.join(HERE, "data", "backfill_seed.db")   # 저장소에 포함된 과거 채움 결과 (PC 에서 backfill 안 돌려도 비교 가능)
METHODS = {"M0": "무작위 (기준선)", "M1": "급등 차트 유사", "M2": "횡보 + 거래량 증가", "M3": "이평선 골든크로스",
           "M4": "RSI 과매도 반등", "M5": "펀딩+OI 숏스퀴즈", "M6": "BTC 대비 상대강도"}
CAP = CAP_M1 = 6          # 방식별 스캔당 선정 종목 수
HOLD, SL, TP, FEE = 72, 0.08, 0.15, 0.002
# 청산 규칙 격자 (가상 매매 탭에서 선택): "손절-익절-최대보유h"
EXIT_SL, EXIT_TP, EXIT_HOLD = (8, 12), (15, 20, 25, 30, 40, 50, 75, 100), (72, 168)
EXIT_GRID = {f"{sl}-{tp}-{h}": dict(sl=sl / 100, tp=tp / 100, hold=h) for sl in EXIT_SL for tp in EXIT_TP for h in EXIT_HOLD}
DEFAULT_RULE = "8-15-72"
MAXH = max(EXIT_HOLD)
DEDUP_H = 24


# ------------------------------------------------------------------ DB
def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY, source TEXT, scan_t TEXT, method TEXT, sub TEXT, symbol TEXT,
        price REAL, score REAL, btc_ok INTEGER, detail TEXT,
        r4 REAL, r24 REAL, r72 REAL, mfe REAL, mae REAL, trade REAL, hit30 INTEGER, bars INTEGER DEFAULT 0,
        UNIQUE(source, method, symbol, scan_t))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_sig ON signals(source, method, symbol, scan_t)")
    c.execute("""CREATE TABLE IF NOT EXISTS exits(signal_id INTEGER, rule TEXT, ret REAL, status TEXT, held INTEGER,
                 PRIMARY KEY(signal_id, rule))""")
    have = {r[1] for r in c.execute("PRAGMA table_info(signals)")}
    for col, typ in [("xbars", "INTEGER DEFAULT 0"), ("status", "TEXT"), ("exit_price", "REAL"), ("exit_t", "TEXT"), ("last_price", "REAL"),
                     ("last_t", "TEXT"), ("rank", "INTEGER"), ("png", "TEXT")]:
        if col not in have:
            c.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
    return c


GROUPS = {"api": "실시간 기록", "backfill": "하락장 60일 (26.09~11)", "bull": "상승장 (24.11~25.07)", "range23": "횡보장 (23.03~10)", "all": "3국면 합침"}
HIST = ["backfill", "bull", "range23"]          # "all" = 이 세 과거 채움 국면을 합친 것


def _grp(x):
    """그룹/출처 이름 → 같은 묶음으로 보는 source 들. 실시간·하락장 채움·상승장 채움을 섞지 않음."""
    if x == "api":
        return ("api",)
    if x in ("backfill", "archive"):
        return ("archive", "backfill")
    if x.startswith("backfill_"):
        return (x,)
    return (f"backfill_{x}",)


def src_name(group):
    """과거 채움 그룹이 DB 에 쓰는 source 이름."""
    return "backfill" if group in ("backfill", "archive") else f"backfill_{group}"


def seed_path(group):
    return SEED if group in ("backfill", "archive") else os.path.join(HERE, "data", f"backfill_{group}_seed.db")


def record(con, source, t, sigs):
    ts = str(t)
    lo = str(t - pd.Timedelta(hours=DEDUP_H))
    g = _grp(source)
    n = 0
    for s in sigs:
        q = f"SELECT 1 FROM signals WHERE method=? AND symbol=? AND scan_t>? AND scan_t<? AND source IN ({','.join('?' * len(g))})"
        if s["method"] != "M0" and con.execute(q, (s["method"], s["symbol"], lo, ts, *g)).fetchone():
            continue      # 같은 방식·종목 24h 중복 제외 (M0 무작위 기준선은 표본이 줄지 않게 제외 안 함)
        cur = con.execute("INSERT OR IGNORE INTO signals(source,scan_t,method,sub,symbol,price,score,btc_ok,detail,rank,png,status,last_price,last_t)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (source, ts, s["method"], s.get("sub", ""), s["symbol"], float(s["price"]),
                           None if s.get("score") is None else float(s["score"]), int(s["btc_ok"]),
                           json.dumps(ps._clean(s.get("detail", {})), ensure_ascii=False), s.get("rank"), s.get("png"),
                           "open", float(s["price"]), ts))
        n += cur.rowcount
    con.commit()
    return n


# ------------------------------------------------------------------ 방식
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def btc_state(btc):
    """btc_ok = 전일까지의 일봉 종가 > 20일 SMA, m6_ok = 7일 고점 대비 -3% 이하로 눌렸다가 1h EMA20 위."""
    now = btc.index[-1] + pd.Timedelta(hours=1)
    d = btc.c[btc.index < now.normalize()].resample("1D").last().dropna()
    ok = bool(len(d) >= 21 and d.iloc[-1] > d.iloc[-20:].mean())
    hi7 = btc.h.iloc[-168:].max()
    dd = btc.c.iloc[-1] / hi7 - 1
    m6 = bool(btc.l.iloc[-168:].min() / hi7 - 1 <= -0.03 and btc.c.iloc[-1] > ema(btc.c, 20).iloc[-1])
    return ok, m6, dd


def sig(method, s, score, sub="", **detail):
    k = s["_k"]["1h"]
    return dict(method=method, symbol=s["symbol"], price=float(k.c.iloc[-1]), score=score, sub=sub, detail=detail)


def m1(patterns, snaps):
    if not patterns:
        return [], []
    fr = pd.DataFrame([s["feat"] for s in snaps])
    fscale = {f: float(fr[f].std()) for f in ps.FEATS if f in fr and fr[f].notna().sum() > 10}
    # 패턴마다 후보 전체의 점수를 z-점수로 바꿔 비교 → 특징이 평범한 패턴 하나가 모든 후보를 '닮음'으로 가져가는 것 방지
    sims = [[ps.similarity(p, s, fscale) for p in patterns] for s in snaps]
    sc = np.array([[x[0] for x in row] for row in sims])
    z = (sc - sc.mean(0)) / np.where(sc.std(0) > 0, sc.std(0), 1)
    rows = []
    for i, s in enumerate(snaps):
        j = int(np.argmax(z[i]))
        scr, shp, fs, per = sims[i][j]
        rows.append((scr, shp, fs, per, patterns[j], s, float(z[i, j])))
    rows.sort(key=lambda x: -x[6])
    out, used = [], {}
    for scr, shp, fs, per, p, s, zz in rows:           # 한 패턴에서 최대 2종목
        key = (p["symbol"], p["launch"])
        if used.get(key, 0) >= 2:
            continue
        used[key] = used.get(key, 0) + 1
        out.append(sig("M1", s, zz, like=p["symbol"], like_launch=p["launch"], sim=scr, shape=shp, feat=fs,
                       **{f"corr_{k}": v for k, v in per.items()}))
        if len(out) >= CAP_M1:
            break
    return out, [r[:6] for r in rows]


def m2(snaps, a):
    k1s = {s["symbol"]: s["_k"]["1h"] for s in snaps}
    allr, sel = ps.range_scan(k1s, a.range_hours, a.recent, a.vol_mult, a.max_chg)
    by = {s["symbol"]: s for s in snaps}
    out = []
    if len(sel):
        sel = sel[~sel.below].sort_values("vol_ratio", ascending=False).head(CAP)
        out = [sig("M2", by[r.symbol], r.vol_ratio, sub=r.box, range_pct=r.range_pct, vol_ratio=r.vol_ratio,
                   breakout=r.breakout, move_recent=r.move_recent) for r in sel.itertuples()]
    return out, allr, sel


def m3(snaps):
    out = []
    for s in snaps:
        c = s["_k"]["1h"].c
        e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
        d = (e20 - e50).values
        if d[-1] > 0 and (d[-5:-1] <= 0).any() and c.iloc[-1] > e200.iloc[-1]:
            out.append(sig("M3", s, float(c.iloc[-1] / e200.iloc[-1] - 1), above_ema200=float(c.iloc[-1] / e200.iloc[-1] - 1)))
    return sorted(out, key=lambda x: -x["score"])[:CAP]


def m4(snaps):
    out = []
    for s in snaps:
        k = s["_k"]["1h"]
        r = ps.rsi(k.c).values
        if not (r[-1] >= 30 and np.nanmin(r[-4:-1]) < 30):
            continue
        l = k.l.values
        i1 = len(l) - 12 + int(np.argmin(l[-12:]))
        i0 = len(l) - 48 + int(np.argmin(l[-48:-12]))
        div = bool(l[i1] < l[i0] and r[i1] > r[i0])
        out.append(sig("M4", s, float(-np.nanmin(r[-4:-1])), sub="div" if div else "os", rsi_min=float(np.nanmin(r[-4:-1])), div=div))
    return sorted(out, key=lambda x: (x["sub"] != "div", -x["score"]))[:CAP]


def m5(snaps, src):
    out = []
    for s in snaps:
        f = s["feat"]
        fu = f.get("funding")
        if not (f["ret_7d"] is not None and f["ret_7d"] <= -0.15 and -0.12 <= f["ret_24h"] <= 0.03
                and fu is not None and np.isfinite(fu) and fu <= 0):
            continue
        oi = f.get("oi_chg_24h")
        if oi is None or not np.isfinite(oi):
            if hasattr(src, "oi_allow"):
                src.oi_allow.add(s["symbol"])
            oi = src.oi_chg(s["symbol"], int(pd.Timestamp(s["t"]).value // 1_000_000))
        if oi is not None and np.isfinite(oi) and oi >= 0.10:
            out.append(sig("M5", s, float(oi), oi_chg_24h=float(oi), funding=float(fu), ret_7d=f["ret_7d"]))
    return sorted(out, key=lambda x: -x["score"])[:CAP]


def m6(snaps, btc, m6_ok):
    if not m6_ok:
        return []
    bc = btc.c
    b7 = bc.iloc[-1] / bc.iloc[-169] - 1
    out = []
    for s in snaps:
        c = s["_k"]["1h"].c
        ratio = (c / bc.reindex(c.index)).dropna()
        if len(ratio) < 169 or not np.isfinite(s["feat"]["ret_7d"]):
            continue
        near = ratio.iloc[-1] / ratio.iloc[-168:].max()
        rs7 = s["feat"]["ret_7d"] - b7
        if near >= 0.97 and rs7 >= 0.10:
            out.append(sig("M6", s, float(rs7), rs7=float(rs7), ratio_vs_7d_high=float(near)))
    return sorted(out, key=lambda x: -x["score"])[:CAP]


def prune_library(lib, say=print):
    """예전 버전이 잘못 잡은 패턴(이미 상승 중, launch=peak)을 library_rejected.jsonl 로 옮김."""
    if not os.path.exists(lib):
        return
    keep, bad = [], []
    for line in open(lib, encoding="utf-8"):
        try:
            p = json.loads(line)
        except Exception:
            continue
        (keep if ps.clean_pre(p.get("feat", {}), p.get("launch", ""), p.get("peak", "")) else bad).append(line.rstrip("\n"))
    if bad:
        with open(os.path.join(os.path.dirname(lib), "library_rejected.jsonl"), "a", encoding="utf-8") as fh:
            fh.write("\n".join(bad) + "\n")
        with open(lib, "w", encoding="utf-8") as fh:
            fh.write("\n".join(keep) + ("\n" if keep else ""))
        say(f"  패턴 라이브러리 정리: 상승 직전이 아닌 패턴 {len(bad)}개를 library_rejected.jsonl 로 이동")


def m0(snaps, t):
    rnd = random.Random(int(pd.Timestamp(t).value) ^ (os.getpid() if not getattr(m0, "reproducible", False) else 0))
    pick = rnd.sample(snaps, min(CAP, len(snaps)))
    return [sig("M0", s, None) for s in pick]


# ------------------------------------------------------------------ 스캔 1회
def run_scan(src, a, source, charts=True, quiet=False, report=True):
    say = (lambda *x: None) if quiet else (lambda *x: print(*x, flush=True))
    t_ms = src.now()
    now = pd.Timestamp(t_ms, unit="ms")
    uni = src.universe()
    uni = uni[(uni.qv24 >= a.min_qv) & ~uni.symbol.isin(ps.NOT_COIN)]
    top = uni.sort_values("chg24", ascending=False).head(a.top)
    btc = src.klines("BTCUSDT", "1h", 600)
    btc_ok, m6_ok, btc_dd = btc_state(btc)
    say(f"기준 시각(UTC) {now}  소스 {source}  BTC 20일선 위={btc_ok}  BTC 7일고점대비 {btc_dd:+.1%}")
    say("상승 상위:", ", ".join(f"{r.symbol} {r.chg24:+.1%}" for r in top.itertuples()))

    patterns = []
    for r in top.itertuples():
        k1 = src.klines(r.symbol, "1h", ps.NEED["1h"])
        if k1 is None or len(k1) < a.lookback + 10:
            continue
        if hasattr(src, "oi_allow"):
            src.oi_allow.add(r.symbol)
        got = ps.pump_pattern(src, r.symbol, k1, a.lookback, a.launch_frac)
        if got is None:
            say(f"  {r.symbol}: 상승 직전 상태가 깨끗하지 않아 패턴에서 제외 (이미 상승 중이거나 launch 탐지 실패)")
            continue
        snap, tl, tp, gain, gl = got
        end = int(tl.value // 1_000_000)
        snap.update(kind="pump", launch=str(tl), peak=str(tp), gain=round(gain, 4), gain_from_low=round(gl, 4),
                    chg24=round(r.chg24, 4), recorded=str(now))
        if charts:
            after = {iv: src.klines(r.symbol, iv, ps.WIN[iv], min(end + ps.WIN[iv] * ps.MS[iv], t_ms)) for iv in ps.WIN}
            after = {iv: (k[k.index >= tl] if k is not None else None) for iv, k in after.items()}
            png = os.path.join(ps.PAT_DIR, f"{tl:%Y%m%d_%H%M}_{r.symbol}.png")
            os.makedirs(ps.PAT_DIR, exist_ok=True)
            ps.plot_pattern(snap, png, f"{r.symbol} pump pattern  launch {tl:%m-%d %H:%M} -> peak {tp:%m-%d %H:%M}  +{gain:.0%}",
                            launch=True, after=after)
            snap["png"] = os.path.relpath(png, ps.ROOT).replace(os.sep, "/")
        patterns.append(snap)

    if source != "backfill":
        lib = os.path.join(ps.PAT_DIR, "library.jsonl")
        os.makedirs(ps.PAT_DIR, exist_ok=True)
        prune_library(lib, say)
        seen = {(p["symbol"], p["launch"]) for p in map(json.loads, open(lib, encoding="utf-8"))} if os.path.exists(lib) else set()
        with open(lib, "a", encoding="utf-8") as fh:
            for p in patterns:
                if (p["symbol"], p["launch"]) not in seen:
                    fh.write(json.dumps({k: v for k, v in p.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")
        if a.library and os.path.exists(lib):
            keys = {(p["symbol"], p["launch"]) for p in patterns}
            patterns_cmp = patterns + [p for p in map(json.loads, open(lib, encoding="utf-8"))
                                       if (p["symbol"], p["launch"]) not in keys and ps.clean_pre(p["feat"], p["launch"], p["peak"])]
        else:
            patterns_cmp = patterns
    else:
        patterns_cmp = patterns

    cand = uni[~uni.symbol.isin(set(top.symbol)) & (uni.chg24 < a.max_chg)].symbol.tolist()
    say(f"후보 {len(cand)}종목 차트 수집…")
    with ThreadPoolExecutor(a.workers) as ex:
        snaps = [s for s in ex.map(lambda s: ps._safe(ps.snapshot, src, s), cand) if s is not None]
    snaps = [s for s in snaps if len(s["_k"]["1h"]) >= 24 * 8]

    s1, rows1 = m1(patterns_cmp, snaps)
    s2, allr, sel2 = m2(snaps, a)
    sigs = s1 + s2 + m3(snaps) + m4(snaps) + m5(snaps, src) + m6(snaps, btc, m6_ok) + m0(snaps, now)
    for m in METHODS:
        for j, s in enumerate([x for x in sigs if x["method"] == m], 1):
            s["rank"] = j
    for s in sigs:
        s["btc_ok"] = btc_ok
    cnt = {m: sum(1 for s in sigs if s["method"] == m) for m in METHODS}
    say("신호:", ", ".join(f"{m} {cnt[m]}" for m in METHODS))

    if report:
        stamp = base = now.strftime("%Y%m%d_%H%M")
        i = 1
        while os.path.exists(os.path.join(ps.OUT_DIR, f"run_{stamp}.json")):
            i += 1; stamp = f"{base}_{i}"
        os.makedirs(ps.OUT_DIR, exist_ok=True)
        a.mode = "all6"
        rep = ps.new_report(a, now, stamp, top)
        rep["source"] = source
        rep["btc_ok"] = btc_ok
        rep["patterns"] = [dict(symbol=p["symbol"], chg24=p["chg24"], launch=p["launch"], peak=p["peak"], gain=p["gain"],
                                gain_from_low=p.get("gain_from_low"), png=p.get("png"), feat=ps._clean(p["feat"])) for p in patterns]
        cands = []
        for j, (sc, shp, fs, per, p, s) in enumerate(rows1[: a.pick]):
            r = dict(symbol=s["symbol"], score=round(sc, 3), shape=round(shp, 3), feat=round(fs, 3), like=p["symbol"],
                     like_launch=p["launch"], corr_4h=per["4h"], corr_1h=per["1h"], corr_15m=per["15m"], **ps._clean(s["feat"]))
            if charts and j < a.charts:
                png = os.path.join(ps.OUT_DIR, f"scan_{stamp}_{s['symbol']}.png")
                ps.plot_pattern(s, png, f"{s['symbol']} now  score {sc:.2f}  (similar to {p['symbol']} launch {p['launch'][5:16]})")
                r["png"] = os.path.relpath(png, ps.ROOT).replace(os.sep, "/")
            cands.append(r)
        rep["candidates"] = cands
        rep["n_candidates_scanned"] = len(snaps)
        rows = [ps._clean(r) for r in allr.to_dict("records")] if len(allr) else []
        for r in rows:
            r["selected"] = bool(r["vol_ratio"] >= a.vol_mult and abs(r["move_recent"]) < a.max_chg)
        rep["range"] = dict(hours=a.range_hours, recent=a.recent, vol_mult=a.vol_mult,
                            n3=int((allr.box == "3%").sum()) if len(allr) else 0, n5=len(allr), rows=rows)
        if charts:
            by = {x["symbol"]: x for x in snaps}
            for s in sigs:
                if s["method"] == "M0":
                    continue
                png = os.path.join(ps.OUT_DIR, f"pick_{stamp}_{s['method']}_{s['symbol']}.png")
                try:
                    ps.plot_pattern(by[s["symbol"]], png, f"{s['symbol']}  {s['method']} pick #{s['rank']}  @ {now:%m-%d %H:%M} UTC")
                    s["png"] = os.path.relpath(png, ps.ROOT).replace(os.sep, "/")
                except Exception as e:
                    print("  chart fail", s["symbol"], e)
        rep["signals"] = [dict(method=s["method"], sub=s["sub"], symbol=s["symbol"], price=s["price"], rank=s.get("rank"),
                               score=s["score"], png=s.get("png"), detail=ps._clean(s["detail"])) for s in sigs]
        ps.save_report(rep, stamp)
    return now, sigs


# ------------------------------------------------------------------ 성과
def sim_exit(h, l, c, rule):
    """h,l,c = 진입가 대비 비율. 손절 먼저(보수적). 반환 (순수익률, 상태, 보유시간h). 아직 안 끝났으면 상태 open."""
    sl, tp, hold = rule["sl"], rule["tp"], rule["hold"]
    n = min(hold, len(c))
    for i in range(n):
        if l[i] <= 1 - sl:
            return -sl - FEE, "sl", i + 1
        if h[i] >= 1 + tp:
            return tp - FEE, "tp", i + 1
    if len(c) >= hold:
        return c[hold - 1] - 1 - FEE, "time", hold
    return (c[-1] - 1 - FEE if len(c) else -FEE), "open", len(c)


def exits_for(k, p0):
    k = k.iloc[:MAXH]
    h, l, c = k.h.values / p0, k.l.values / p0, k.c.values / p0
    return {name: sim_exit(h, l, c, r) for name, r in EXIT_GRID.items()}


def outcome(k, p0):
    """k: 신호 이후 1h 봉 (첫 봉 = 신호 시각에 열린 봉)."""
    k = k.iloc[:HOLD]
    n = len(k)
    if n == 0:
        return None
    h, l, c = k.h.values, k.l.values, k.c.values
    res = dict(bars=n, r4=c[3] / p0 - 1 if n >= 4 else None, r24=c[23] / p0 - 1 if n >= 24 else None,
               r72=c[71] / p0 - 1 if n >= 72 else None, mfe=h.max() / p0 - 1, mae=l.min() / p0 - 1)
    tr, st, xi = None, "open", None
    for i in range(n):
        if l[i] <= p0 * (1 - SL):
            tr, st, xi = -SL, "sl", i; break
        if h[i] >= p0 * (1 + TP):
            tr, st, xi = TP, "tp", i; break
    if tr is None and n >= HOLD:
        tr, st, xi = c[-1] / p0 - 1, "time", n - 1
    res["trade"] = None if tr is None else tr - FEE
    res["status"] = st
    res["exit_price"] = None if tr is None else p0 * (1 + tr)
    res["exit_t"] = None if xi is None else str(k.index[xi] + pd.Timedelta(hours=1))
    res["last_price"] = float(c[-1])
    res["last_t"] = str(k.index[-1] + pd.Timedelta(hours=1))
    res["hit30"] = 1 if res["mfe"] >= 0.30 else (0 if n >= HOLD else None)
    return res


def _refresh_prices(con, src, now):
    """실시간: 아직 열린 포지션은 바이낸스 현재가(티커)로 평가."""
    if not hasattr(src, "prices") or not isinstance(src, ps.BinanceAPI):
        return 0
    px = src.prices()
    n = 0
    for i, sym in con.execute("SELECT id, symbol FROM signals WHERE (status IS NULL OR status='open') AND source='api'").fetchall():
        if sym in px:
            con.execute("UPDATE signals SET last_price=?, last_t=?, status=COALESCE(status,'open') WHERE id=?", (px[sym], str(now), i))
            n += 1
    return n


def update(con, src, source):
    g = _grp(source)
    now = pd.Timestamp(src.now(), unit="ms")
    q = (f"SELECT id, symbol, scan_t, price FROM signals WHERE (bars < {HOLD} OR COALESCE(xbars,0) < {MAXH}) "
         f"AND source IN ({','.join('?' * len(g))}) AND scan_t <= ?")
    rows = pd.DataFrame(con.execute(q, (*g, str(now - pd.Timedelta(hours=1)))).fetchall(), columns=["id", "symbol", "scan_t", "price"])
    n_px = 0
    if rows.empty:
        n_px = _refresh_prices(con, src, now)
        con.commit()
        print(f"  현재가 갱신 {n_px}건 (1시간봉이 닫힌 신호가 아직 없어 성과 계산은 0건)")
        return 0
    rows["t"] = pd.to_datetime(rows.scan_t)

    def one(sym):
        r = rows[rows.symbol == sym]
        k = src.klines_since(sym, int(r.t.min().value // 1_000_000))
        out = []
        if k is None or k.empty:
            return out
        for x in r.itertuples():
            kk = k[k.index >= x.t]
            res = outcome(kk, x.price)
            if res:
                res["xbars"] = min(len(kk), MAXH)
                res["exits"] = exits_for(kk, x.price)
                out.append((x.id, res))
        return out

    with ThreadPoolExecutor(8) as ex:
        allres = [y for ys in ex.map(lambda s: ps._safe(one, s) or [], rows.symbol.unique()) for y in ys]
    for i, r in allres:
        con.execute("UPDATE signals SET bars=?, r4=?, r24=?, r72=?, mfe=?, mae=?, trade=?, hit30=?, status=?, exit_price=?, exit_t=?,"
                    " last_price=?, last_t=? WHERE id=?",
                    (r["bars"], r["r4"], r["r24"], r["r72"], r["mfe"], r["mae"], r["trade"], r["hit30"], r["status"],
                     r["exit_price"], r["exit_t"], r["last_price"], r["last_t"], i))
        con.execute("UPDATE signals SET xbars=? WHERE id=?", (r["xbars"], i))
        con.executemany("INSERT OR REPLACE INTO exits(signal_id, rule, ret, status, held) VALUES(?,?,?,?,?)",
                        [(i, nm, float(v[0]), v[1], int(v[2])) for nm, v in r["exits"].items()])
    n_px = _refresh_prices(con, src, now)
    con.commit()
    print(f"  현재가 갱신 {n_px}건")
    return len(allres)


# ------------------------------------------------------------------ 비교
def load(con, group="backfill"):
    if group == "all":
        parts = [load(con, g).assign(regime=g) for g in HIST]
        parts = [p for p in parts if len(p)]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    g = _grp(group)
    df = pd.read_sql_query(f"SELECT * FROM signals WHERE source IN ({','.join('?' * len(g))})", con, params=g)
    sp, sn = seed_path(group), src_name(group)
    if group != "api" and not (df.source == sn).any() and os.path.exists(sp):
        with sqlite3.connect(sp) as sc:
            seed = pd.read_sql_query("SELECT * FROM signals WHERE source=?", sc, params=(sn,))
        df = pd.concat([seed, df], ignore_index=True)
    return df


def load_exits(con, group, rule):
    """규칙 하나의 신호별 결과 (local DB, 없으면 seed)."""
    if group == "all":
        parts = [load_exits(con, g, rule) for g in HIST]
        parts = [p for p in parts if len(p)]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    g = _grp(group)
    q = (f"SELECT e.signal_id AS id, e.ret AS xret, e.status AS xstatus, e.held AS xheld FROM exits e JOIN signals s ON s.id=e.signal_id "
         f"WHERE e.rule=? AND s.source IN ({','.join('?' * len(g))})")
    ex = pd.read_sql_query(q, con, params=(rule, *g))
    if group != "api" and ex.empty and os.path.exists(seed_path(group)):
        with sqlite3.connect(seed_path(group)) as sc:
            try:
                ex = pd.read_sql_query("SELECT signal_id AS id, ret AS xret, status AS xstatus, held AS xheld FROM exits WHERE rule=?",
                                       sc, params=(rule,))
            except Exception:
                pass
    return ex


def tp_table(con, group, df, sl, hold):
    """같은 손절·보유시간에서 익절 라인별 방식 평균 (청산 완료 거래만)."""
    out = {}
    for tp in EXIT_TP:
        rule = f"{sl}-{tp}-{hold}"
        ex = load_exits(con, group, rule)
        if ex.empty:
            continue
        d = df[["id", "method"]].merge(ex, on="id")
        d = d[d.xstatus != "open"]
        out[tp] = {m: dict(avg=float(x.xret.mean()), n=len(x), hit=float((x.xstatus == "tp").mean()))
                   for m, x in d.groupby("method")}
    return out


def export_seed(group="backfill"):
    """과거 채움 결과만 seed 파일로 복사 (저장소 커밋용)."""
    sp, sn = seed_path(group), src_name(group)
    if os.path.exists(sp):
        os.remove(sp)
    with sqlite3.connect(DB) as c:
        c.execute("ATTACH DATABASE ? AS s", (sp,))
        c.execute("CREATE TABLE s.signals AS SELECT * FROM main.signals WHERE source=?", (sn,))
        c.execute("CREATE TABLE s.exits AS SELECT e.* FROM main.exits e JOIN main.signals x ON x.id=e.signal_id WHERE x.source=?", (sn,))
        n = c.execute("SELECT COUNT(*) FROM s.signals").fetchone()[0]
        c.commit()
    with sqlite3.connect(sp) as c:
        c.execute("VACUUM")
    print(f"seed 저장 {n}건 → {os.path.relpath(sp, ps.ROOT)}")


def compare(df, btc_only=False):
    if df.empty:
        return []
    if btc_only:
        df = df[df.btc_ok == 1]
    base = df[(df.method == "M0") & df.r72.notna()]
    b72 = base.r72.mean() if len(base) else np.nan
    out = []
    for m, name in METHODS.items():
        for sub in [None] + sorted(x for x in df[df.method == m]["sub"].dropna().unique() if x):
            d = df[(df.method == m) & ((df["sub"] == sub) if sub else True)]
            d72 = d[d.r72.notna()]
            dt = d[d.trade.notna()]
            out.append(dict(method=m, name=name, sub=sub or "", n=len(d), n72=len(d72),
                            scans=int(d.scan_t.nunique()),
                            r4=_m(d.r4), r24=_m(d.r24), r72=_m(d72.r72), r72_med=_md(d72.r72),
                            win72=_m(d72.r72 > 0) if len(d72) else None, excess72=(_m(d72.r72) - b72) if len(d72) and np.isfinite(b72) else None,
                            trade=_m(dt.trade), trade_win=_m(dt.trade > 0) if len(dt) else None,
                            avg_win=_m(dt.trade[dt.trade > 0]), avg_loss=_m(dt.trade[dt.trade <= 0]),
                            payoff=(_m(dt.trade[dt.trade > 0]) / -_m(dt.trade[dt.trade <= 0]))
                            if (dt.trade > 0).any() and (dt.trade <= 0).any() else None,
                            pf=float(dt.trade[dt.trade > 0].sum() / -dt.trade[dt.trade <= 0].sum())
                            if (dt.trade <= 0).any() and dt.trade[dt.trade <= 0].sum() < 0 else None,
                            trade_sum=float(dt.trade.sum()) if len(dt) else None,
                            hit30=_m(d.hit30.dropna()), mfe_med=_md(d72.mfe), mae_med=_md(d72.mae)))
    return out


def regime_table(df, btc_only=False):
    """방식 × 국면별 거래 성과 (3국면 합침 화면용)."""
    if df.empty or "regime" not in df:
        return []
    out = []
    for g in HIST + ["all"]:
        d = df if g == "all" else df[df.regime == g]
        if d.empty:
            continue
        for r in compare(d, btc_only):
            if not r["sub"]:
                out.append(dict(regime=g, method=r["method"], n=r["n"], trade=r["trade"], trade_win=r["trade_win"],
                                payoff=r["payoff"], pf=r["pf"], excess72=r["excess72"]))
    return out


def _m(s):
    s = pd.Series(s).dropna().astype(float)
    return float(s.mean()) if len(s) else None


def _md(s):
    s = pd.Series(s).dropna().astype(float)
    return float(s.median()) if len(s) else None


def paper(df, amount=100.0, method=None, days=0, btc_only=False, ex=None):
    """가상 매매: 신호마다 amount USDT 매수, 손절/익절/72h 규칙으로 청산. 미청산은 현재가로 평가."""
    if df.empty:
        return dict(summary=[], open=[], closed=[], curve={})
    df = df.copy()
    df["t"] = pd.to_datetime(df.scan_t)
    if days:
        df = df[df.t >= df.t.max() - pd.Timedelta(days=days)]
    if method:
        df = df[df.method == method]
    if btc_only:
        df = df[df.btc_ok == 1]
    if df.empty:
        return dict(summary=[], open=[], closed=[], curve={}, amount=amount)
    df["stale"] = df.last_price.isna()            # 아직 현재가 갱신 전 (선정가로 평가)
    df["last_price"] = df.last_price.fillna(df.price)
    if ex is not None:                              # 다른 청산 규칙: 규칙별 결과로 덮어씀
        df = df.merge(ex, on="id", how="left")
        df["status"] = df.xstatus.fillna("open")
        df["closed"] = df.status != "open"
        df["trade"] = np.where(df.closed, df.xret, np.nan)
        df["exit_t"] = np.where(df.closed, (df.t + pd.to_timedelta(df.xheld.fillna(0), unit="h")).astype(str), None)
        df["exit_price"] = np.where(df.closed, df.price * (1 + df.xret + FEE), np.nan)
    else:
        df["closed"] = df.trade.notna()
        df["status"] = df.status.fillna("open")
    df["ret"] = np.where(df.closed, df.trade, df.last_price / df.price - 1 - FEE)
    df["pnl"] = df.ret * amount
    summ = []
    for m, name in METHODS.items():
        d = df[df.method == m]
        if d.empty:
            continue
        c, o = d[d.closed], d[~d.closed]
        summ.append(dict(method=m, name=name, trades=len(d), open=len(o), closed=len(c), invested=len(d) * amount,
                         realized=float(c.pnl.sum()), unrealized=float(o.pnl.sum()), total=float(d.pnl.sum()),
                         ret=float(d.pnl.sum() / (len(d) * amount)), win=float((c.ret > 0).mean()) if len(c) else None,
                         tp=int((c.status == "tp").sum()), sl=int((c.status == "sl").sum()), time=int((c.status == "time").sum()),
                         stale=int(d.stale.sum()), best=_m([d.ret.max()]), worst=_m([d.ret.min()])))
    curve = {}
    c = df[df.closed & df.exit_t.notna()].copy()
    if len(c):
        c["xt"] = pd.to_datetime(c.exit_t).dt.floor("D")
        for m, d in c.groupby("method"):
            s = d.groupby("xt").pnl.sum().cumsum()
            curve[m] = [[str(t)[:10], round(float(v), 2)] for t, v in s.items()]
    cols = ["id", "scan_t", "method", "sub", "symbol", "rank", "price", "last_price", "last_t", "exit_price", "exit_t",
            "status", "ret", "pnl", "btc_ok", "mfe", "mae", "png", "stale"]
    op = df[~df.closed].sort_values("scan_t", ascending=False)[cols].head(300)
    cl = df[df.closed].sort_values("exit_t", ascending=False)[cols].head(300)
    js = lambda x: json.loads(x.to_json(orient="records"))
    return dict(summary=summ, open=js(op), closed=js(cl), curve=curve, amount=amount)


def write_compare(con, group):
    df = load(con, group)
    L = [f"# 6가지 방식 비교 ({GROUPS.get(group, group)})", ""]
    if df.empty:
        L.append("신호 없음")
    else:
        L += [f"기간 {df.scan_t.min()[:16]} ~ {df.scan_t.max()[:16]} UTC, 스캔 {df.scan_t.nunique()}회, 신호 {len(df)}건. "
              f"성과는 신호 시점 종가 기준, 거래 = 손절 -8% / 익절 +15% / 72h, 수수료 0.2%.", ""]
        for bo in (False, True):
            L += [f"## {'BTC 20일선 위일 때만' if bo else '전체'}", "",
                  "| 방식 | 세부 | 신호 | 72h 완료 | 24h 평균 | 72h 평균 | 72h 중앙 | 72h 승률 | 기준선 대비 | 거래 평균 | 거래 승률 | +30% 도달 | MFE 중앙 | MAE 중앙 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for r in compare(df, bo):
                L.append(f"| {r['method']} {r['name']} | {r['sub']} | {r['n']} | {r['n72']} | {_p(r['r24'])} | {_p(r['r72'])} | {_p(r['r72_med'])} | "
                         f"{_p(r['win72'], 0, False)} | {_p(r['excess72'])} | {_p(r['trade'])} | {_p(r['trade_win'], 0, False)} | "
                         f"{_p(r['hit30'], 1, False)} | {_p(r['mfe_med'])} | {_p(r['mae_med'])} |")
            L.append("")
    path = os.path.join(ps.OUT_DIR, f"compare_{group}.md")
    os.makedirs(ps.OUT_DIR, exist_ok=True)
    open(path, "w", encoding="utf-8").write("\n".join(L))
    print("\n".join(L))
    print("저장:", os.path.relpath(path, ps.ROOT))


def _p(v, d=1, sign=True):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "–"
    return f"{v*100:+.{d}f}%" if sign else f"{v*100:.{d}f}%"


# ------------------------------------------------------------------ 과거 채움 (아카이브)
class Backfill(ps.Archive):
    """공개 아카이브 데이터를 한 번에 메모리에 올려 두고, 시각 t 를 옮겨 가며 '그때의 지금' 으로 스캔."""

    def __init__(self, start, end, workers=32):
        super().__init__()
        rb = self.rb
        self.t = pd.Timestamp(end)
        self.oi_allow = set()
        lo = pd.Timestamp(start) - pd.Timedelta(days=30)
        syms = [s for s in rb.list_symbols() if s not in ps.NOT_COIN]
        print(f"아카이브 1h 로드 ({len(syms)}종목)…", flush=True)
        self.h1, self.h4, self.m15, self.fund = {}, {}, {}, {}

        def load1(s):
            path = os.path.join(rb.H1_DIR, f"{s}.npz")
            if not os.path.exists(path):
                return s, None, None
            k, f = rb.load_1h(s)
            k = k[(k.index >= lo) & (k.qv > 0)].drop(columns="tqv")
            return s, k, f

        with ThreadPoolExecutor(workers) as ex:
            for s, k, f in ex.map(load1, syms):
                if k is not None and len(k) and k.index[-1] >= pd.Timestamp(start) and k.index[0] <= pd.Timestamp(end):
                    self.h1[s], self.fund[s] = k, f
        missing = [s for s in syms if s not in self.h1 and not os.path.exists(os.path.join(rb.H1_DIR, f"{s}.npz"))]
        if missing:
            print(f"  1h 캐시 없는 종목 {len(missing)}개는 건너뜀 (먼저 backtest/regime_bt.py fetch 실행 시 포함)", flush=True)
        for s, k in self.h1.items():
            self.h4[s] = k.resample("4h").agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum", "qv": "sum"}).dropna()
        days = pd.date_range((pd.Timestamp(start) - pd.Timedelta(days=1)).normalize(), pd.Timestamp(end).normalize())
        print(f"15m 로드 ({len(self.h1)}종목 × {len(days)}일, 처음엔 다운로드로 오래 걸림)…", flush=True)

        def load15(s):
            k = rb.load_15m(s, days)
            return s, (k.drop(columns="tqv") if k is not None else None)

        done = 0
        with ThreadPoolExecutor(workers) as ex:
            for s, k in ex.map(load15, list(self.h1)):
                if k is not None:
                    self.m15[s] = k
                done += 1
                if done % 100 == 0:
                    print(f"  15m {done}/{len(self.h1)}", flush=True)

    def set(self, t):
        self.t = pd.Timestamp(t)
        self.oi_allow = set()

    def now(self):
        return int(self.t.value // 1_000_000)

    def _frame(self, sym, iv):
        return {"1h": self.h1, "4h": self.h4, "15m": self.m15}[iv].get(sym)

    def klines(self, sym, iv, limit, end_ms=None):
        f = self._frame(sym, iv)
        if f is None:
            return None
        end = pd.Timestamp(end_ms or self.now(), unit="ms") - pd.Timedelta(milliseconds=ps.MS[iv])
        i = f.index.searchsorted(end, side="right")
        return f.iloc[max(0, i - limit):i]

    def klines_since(self, sym, start_ms):
        f = self.h1.get(sym)
        if f is None:
            return None
        return f[(f.index >= pd.Timestamp(start_ms, unit="ms")) & (f.index + pd.Timedelta(hours=1) <= self.t)]

    def universe(self):
        rows = []
        last = self.t - pd.Timedelta(hours=1)
        for s, k in self.h1.items():
            i = k.index.searchsorted(last, side="right")
            if i < 25 or k.index[i - 1] != last:
                continue
            w = k.iloc[i - 25:i]
            rows.append(dict(symbol=s, chg24=w.c.iloc[-1] / w.c.iloc[0] - 1, qv24=w.qv.iloc[1:].sum()))
        return pd.DataFrame(rows)

    def funding(self, sym, end_ms=None):
        f = self.fund.get(sym)
        if f is None or not len(f):
            return np.nan
        f = f[f.index <= pd.Timestamp(end_ms or self.now(), unit="ms")]
        return float(f.iloc[-1]) if len(f) else np.nan

    def oi_chg(self, sym, end_ms=None):
        if sym not in self.oi_allow:
            return np.nan
        return super().oi_chg(sym, end_ms)


# ------------------------------------------------------------------ CLI
def add_klines_since():
    """실시간/아카이브 소스에 '신호 이후 1h 봉' 조회 추가."""
    def api_since(self, sym, start_ms):
        d = self._get("/fapi/v1/klines", symbol=sym, interval="1h", startTime=int(start_ms), limit=1000)
        if not d:
            return None
        k = pd.DataFrame([[float(x[0]), *map(float, x[1:6]), float(x[7]), float(x[6])] for x in d],
                         columns=["ts", "o", "h", "l", "c", "v", "qv", "ct"])
        k = k[k.ct < self.now()]
        k.index = pd.to_datetime(k.pop("ts").astype("int64"), unit="ms")
        return k.drop(columns="ct")

    def arc_since(self, sym, start_ms):
        k, _ = self._h1(sym)
        if k is None:
            return None
        return k[(k.index >= pd.Timestamp(start_ms, unit="ms")) & (k.index + pd.Timedelta(hours=1) <= self.end)]

    def api_prices(self):
        d = self._get("/fapi/v1/ticker/price") or []
        return {x["symbol"]: float(x["price"]) for x in d}

    ps.BinanceAPI.klines_since = api_since
    ps.BinanceAPI.prices = api_prices
    ps.Archive.klines_since = arc_since


def main():
    ap = argparse.ArgumentParser(description="6가지 방식 신호 기록 / 성과 추적 / 비교")
    ap.add_argument("cmd", choices=["scan", "update", "backfill", "report", "export-seed"])
    ap.add_argument("--source", choices=["api", "archive"], default="api")
    ap.add_argument("--days", type=int, default=60, help="backfill: 과거 며칠")
    ap.add_argument("--every", type=int, default=4, help="backfill: 몇 시간마다 스캔")
    ap.add_argument("--end", default="", help="backfill: 마지막 시각 (기본 아카이브 마지막 날 다음 0시)")
    ap.add_argument("--group", default="", help="report/export-seed: api / backfill(하락장 60일) / bull 등 과거 채움 이름")
    ap.add_argument("--tag", default="", help="backfill: 결과를 따로 묶을 이름 (예: bull). 비우면 기본 과거 채움")
    ap.add_argument("--start", default="", help="backfill: 시작 시각 (주면 --days 대신 사용)")
    for k, v, t in [("top", 5, int), ("lookback", 72, int), ("launch-frac", 0.25, float), ("pick", 20, int), ("charts", 10, int),
                    ("min-qv", 2e6, float), ("max-chg", 0.15, float), ("range-hours", 24, int), ("recent", 4, int),
                    ("vol-mult", 2.0, float), ("workers", 8, int)]:
        ap.add_argument("--" + k, type=t, default=v)
    ap.add_argument("--library", action="store_true")
    ap.add_argument("--mode", default="all6")
    a = ap.parse_args()
    add_klines_since()
    ps.setup_fonts()
    con = db()

    if a.cmd == "scan":
        src = ps.BinanceAPI() if a.source == "api" else ps.Archive()
        now, sigs = run_scan(src, a, a.source)
        n = record(con, a.source, now, sigs)
        print(f"신호 {n}건 기록 (24h 안 중복 제외)")
        print(f"지난 신호 성과 갱신 {update(con, src, a.source)}건")
        write_compare(con, "api" if a.source == "api" else "backfill")
    elif a.cmd == "update":
        src = ps.BinanceAPI() if a.source == "api" else ps.Archive()
        print(f"성과 갱신 {update(con, src, a.source)}건")
        prune_library(os.path.join(ps.PAT_DIR, "library.jsonl"))
    elif a.cmd == "export-seed":
        export_seed(a.group or "backfill")
    elif a.cmd == "report":
        write_compare(con, a.group or ("api" if a.source == "api" else "backfill"))
    else:
        rb_end = pd.Timestamp(__import__("regime_bt").archive_end()) + pd.Timedelta(days=1)
        end = pd.Timestamp(a.end) if a.end else rb_end
        start = pd.Timestamp(a.start) if a.start else end - pd.Timedelta(days=a.days)
        group = a.tag or "backfill"
        sn = src_name(group)
        src = Backfill(start, end, workers=max(16, a.workers))
        m0.reproducible = True
        times = pd.date_range(start, end, freq=f"{a.every}h")
        have = {r[0] for r in con.execute("SELECT DISTINCT scan_t FROM signals WHERE source=?", (sn,))}
        print(f"과거 채움 [{group}] {start} ~ {end}, {a.every}시간마다 {len(times)}회", flush=True)
        t0 = time.time()
        for i, t in enumerate(times):
            if str(t) in have:
                continue
            src.set(t)
            try:
                now, sigs = run_scan(src, a, "backfill", charts=False, quiet=True, report=False)
            except Exception as e:
                print(f"  {t} 실패: {type(e).__name__} {e}", flush=True)
                continue
            n = record(con, sn, now, sigs)
            el = time.time() - t0
            print(f"  [{i+1}/{len(times)}] {t:%m-%d %H:%M} 신호 {n}  ({el/60:.1f}분 경과)", flush=True)
        src.set(min(rb_end, end + pd.Timedelta(days=8)))       # 마지막 신호도 168h 뒤까지 결과 계산
        print(f"성과 계산 {update(con, src, sn)}건")
        write_compare(con, group)


if __name__ == "__main__":
    main()
