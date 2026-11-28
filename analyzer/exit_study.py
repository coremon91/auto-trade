"""청산 규칙 비교 — 같은 신호(진입)에 청산 규칙만 바꿔서 성과 비교.

  python analyzer/exit_study.py                 # backfill_seed.db (또는 tracker.db) 의 과거 채움 신호 사용
  python analyzer/exit_study.py --group api     # 실시간 기록 신호 (72h+ 지난 것만)

진입 = 신호 시점 종가. 이후 1h 봉으로 시뮬레이션, 수수료 왕복 0.2%.
같은 봉에서 손절과 익절/절반익절이 둘 다 닿으면 손절 먼저(보수적). 트레일링·본전 스탑은 다음 봉부터 적용.
모든 규칙이 같은 표본을 쓰도록, 가장 긴 보유시간(168h)만큼 이후 봉이 있는 신호만 사용.
과최적화 점검: 기간을 앞/뒤 절반으로 나눠 각각 성과를 따로 보여줌.
결과: analyzer/out/exit_study.md, analyzer/data/exit_study_<group>.json (대시보드가 읽음)
"""
from __future__ import annotations

import argparse, json, os, sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tracker as tk  # noqa: E402
import pattern_scan as ps  # noqa: E402

FEE = tk.FEE
MAXH = 168

# 이름: (손절, 익절, 최대보유h, 트레일 활성, 트레일 폭, 본전 이동 기준, 절반 익절 기준)
RULES = {
    "R0 현행 (손절8 익절15 72h)":              dict(sl=.08, tp=.15, hold=72),
    "R1 손절12 익절15 72h":                    dict(sl=.12, tp=.15, hold=72),
    "R2 손절12 익절30 72h":                    dict(sl=.12, tp=.30, hold=72),
    "R3 손절15 익절30 120h":                   dict(sl=.15, tp=.30, hold=120),
    "R4 손절 없음 72h 보유":                   dict(sl=None, tp=None, hold=72),
    "R5 손절12 + 10%부터 12% 트레일 120h":     dict(sl=.12, hold=120, act=.10, trail=.12),
    "R6 손절10 + 8%부터 8% 트레일 72h":        dict(sl=.10, hold=72, act=.08, trail=.08),
    "R7 X2형 (손절10, +8% 본전, +15% 절반, 나머지 12% 트레일) 120h": dict(sl=.10, hold=120, be=.08, half=.15, trail=.12, act=.15),
    "R8 손절12, +30% 절반, 나머지 15% 트레일 168h": dict(sl=.12, hold=168, half=.30, trail=.15, act=.30),
    "R9 단타 (손절6 익절10 24h)":              dict(sl=.06, tp=.10, hold=24),
    "R10 손절8 익절25 120h":                   dict(sl=.08, tp=.25, hold=120),
    "R11 손절 없음 24h 보유":                  dict(sl=None, tp=None, hold=24),
}


def sim(h, l, c, rule):
    """h,l,c: 진입 후 1h 봉 (진입가 대비 비율로 정규화된 배열). 반환 (수익률, 보유시간h, 사유)."""
    sl, tp, hold = rule.get("sl"), rule.get("tp"), rule["hold"]
    act, trail, be, half = rule.get("act"), rule.get("trail"), rule.get("be"), rule.get("half")
    stop = (1 - sl) if sl else -np.inf
    peak, done_half, realized, armed = 1.0, False, 0.0, False
    n = min(hold, len(c))
    for i in range(n):
        rem = 0.5 if done_half else 1.0
        if l[i] <= stop:                                   # 손절/트레일/본전 (보수적으로 먼저)
            return realized + rem * (stop - 1), i + 1, ("trail" if armed or stop >= 1 else "sl")
        if tp and h[i] >= 1 + tp:
            return realized + rem * tp, i + 1, "tp"
        if half and not done_half and h[i] >= 1 + half:
            realized += 0.5 * half; done_half = True
        peak = max(peak, h[i])
        # 다음 봉부터 적용할 스탑 갱신
        if be and peak >= 1 + be:
            stop = max(stop, 1.0)
        if trail and act is not None and peak >= 1 + act:
            armed = True
            stop = max(stop, peak * (1 - trail))
    rem = 0.5 if done_half else 1.0
    return realized + rem * (c[n - 1] - 1), n, "time"


def load_bars(sym, start, cache):
    if sym not in cache:
        path = os.path.join(ps.ROOT, "data", "bt", "h1", f"{sym}.npz")
        if not os.path.exists(path):
            cache[sym] = None
        else:
            import regime_bt as rb
            k, _ = rb.load_1h(sym)
            cache[sym] = k[k.qv > 0]
    k = cache[sym]
    if k is None:
        return None
    return k[k.index >= start].iloc[:MAXH]


def run(df):
    sys.path.insert(0, os.path.join(ps.ROOT, "backtest"))
    cache = {}
    df = df.copy()
    df["t"] = pd.to_datetime(df.scan_t)
    rows = []

    def one(sym):
        out = []
        d = df[df.symbol == sym]
        for x in d.itertuples():
            k = load_bars(sym, x.t, cache)
            if k is None or len(k) < MAXH:
                continue
            p0 = x.price
            h, l, c = k.h.values / p0, k.l.values / p0, k.c.values / p0
            r = dict(id=x.id, method=x.method, sub=x.sub, symbol=sym, t=x.t, btc_ok=x.btc_ok)
            for name, rule in RULES.items():
                ret, held, why = sim(h, l, c, rule)
                r[name] = ret - FEE
                r[name + "|h"] = held
                r[name + "|why"] = why
            out.append(r)
        return out

    with ThreadPoolExecutor(16) as ex:
        for o in ex.map(one, df.symbol.unique()):
            rows += o
    return pd.DataFrame(rows)


def summarize(res, btc_only):
    d0 = res[res.btc_ok == 1] if btc_only else res
    mid = d0.t.min() + (d0.t.max() - d0.t.min()) / 2 if len(d0) else None
    out = {}
    for m in tk.METHODS:
        d = d0[d0.method == m]
        if d.empty:
            continue
        out[m] = {}
        for name in RULES:
            v = d[name]
            a, b = d[d.t < mid][name], d[d.t >= mid][name]
            w, lo = v[v > 0].sum(), -v[v <= 0].sum()
            out[m][name] = dict(n=len(v), avg=float(v.mean()), med=float(v.median()), win=float((v > 0).mean()),
                                pf=float(w / lo) if lo > 0 else None, hold=float(d[name + "|h"].mean()),
                                first=float(a.mean()) if len(a) else None, second=float(b.mean()) if len(b) else None,
                                n1=len(a), n2=len(b),
                                why={k: int(c) for k, c in d[name + "|why"].value_counts().items()})
    return out


def pct(v, d=2):
    return "–" if v is None or not np.isfinite(v) else f"{v*100:+.{d}f}%"


def write(res, summ, path):
    names = list(RULES)
    L = ["# 청산 규칙 비교", "",
         f"신호 {len(res)}건 ({res.t.min():%Y-%m-%d} ~ {res.t.max():%Y-%m-%d} UTC, 이후 {MAXH}h 봉이 있는 것만). "
         "진입 = 신호 시점 종가, 수수료 0.2% 차감. 앞/뒤 = 기간을 반으로 나눈 각 구간의 평균 (둘 다 좋아야 우연이 아닐 가능성↑).", ""]
    for key, title in (("all", "전체 신호"), ("btc", "BTC 20일선 위일 때만")):
        L += [f"## {title}", ""]
        S = summ[key]
        # 방식 × 규칙 평균 표
        L += ["| 규칙 | " + " | ".join(S) + " |", "|---|" + "---|" * len(S)]
        for nm in names:
            cells = []
            for m in S:
                r = S[m][nm]
                best = max(S[m].values(), key=lambda x: x["avg"])
                mark = "**" if r is best else ""
                cells.append(f"{mark}{pct(r['avg'])}{mark}")
            L.append(f"| {nm} | " + " | ".join(cells) + " |")
        L += ["", "방식별 상위 3개 규칙 (평균, 승률, PF, 평균 보유, 앞/뒤 구간):", ""]
        for m, rs in S.items():
            top = sorted(rs.items(), key=lambda x: -x[1]["avg"])[:3]
            L.append(f"- **{m} {tk.METHODS[m]}** (n={next(iter(rs.values()))['n']}): " + "; ".join(
                f"{nm.split(' ')[0]} {pct(r['avg'])} 승률 {r['win']*100:.0f}% PF {r['pf'] if r['pf'] is None else round(r['pf'], 2)} "
                f"{r['hold']:.0f}h, 앞 {pct(r['first'])} / 뒤 {pct(r['second'])}" for nm, r in top))
        L.append("")
    open(path, "w", encoding="utf-8").write("\n".join(L))
    print("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", choices=["backfill", "api"], default="backfill")
    a = ap.parse_args()
    con = tk.db()
    df = tk.load(con, a.group)
    if df.empty:
        raise SystemExit("신호 없음")
    res = run(df)
    if res.empty:
        raise SystemExit(f"이후 {MAXH}h 봉이 있는 신호가 없음 (1h 캐시: data/bt/h1)")
    summ = {"all": summarize(res, False), "btc": summarize(res, True)}
    os.makedirs(ps.OUT_DIR, exist_ok=True)
    blob = dict(group=a.group, rules=RULES, n=len(res), start=str(res.t.min()), end=str(res.t.max()), summary=summ)
    for path in (os.path.join(ps.OUT_DIR, "exit_study.json"), os.path.join(HERE, "data", f"exit_study_{a.group}.json")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(blob, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    write(res, summ, os.path.join(ps.OUT_DIR, "exit_study.md"))


if __name__ == "__main__":
    main()
