"""CLI:  python -m screener <command>

  collect   REST 수집 (현재 상장 종목 전체, 이어받기). 크론으로 4시간마다 권장
  backfill  data.binance.vision 아카이브에서 과거 데이터 (상장폐지 종목 포함)
  train     특징/라벨 생성 → 시간 분할 학습/검증 → models/ 저장
  score     최신 데이터로 점수화 → output/watchlist_latest.md
  events    과거 급등 이벤트 목록 (분석용 CSV)
"""
from __future__ import annotations

import argparse
from datetime import date

from .config import Config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="screener")
    ap.add_argument("--db", default=None)
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--out-dir", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect")
    c.add_argument("--days", type=int, default=45, help="첫 수집 시 가져올 기간(일)")
    c.add_argument("--symbols", nargs="*")

    b = sub.add_parser("backfill")
    b.add_argument("--start", required=True, help="YYYY-MM-DD")
    b.add_argument("--end")
    b.add_argument("--symbols", nargs="*")
    b.add_argument("--no-metrics", action="store_true", help="일별 OI/롱숏 파일 생략 (빠름)")

    t = sub.add_parser("train")
    t.add_argument("--horizon", type=int)
    t.add_argument("--pump", type=float, help="급등 기준, 예: 0.2")
    t.add_argument("--dump", type=float)
    t.add_argument("--test-frac", type=float)
    t.add_argument("--no-importance", action="store_true")

    s = sub.add_parser("score")
    s.add_argument("--top", type=int, default=30)
    s.add_argument("--sort", default="p_pump", choices=["p_pump", "p_pump_first", "p_dump"])
    s.add_argument("--min-volume", type=float, default=5e6, help="24h 거래대금 하한(USDT)")

    e = sub.add_parser("events")
    e.add_argument("--out", default="output/pump_events.csv")

    a = ap.parse_args(argv)
    cfg = Config()
    if a.db: cfg.db_path = a.db
    if a.model_dir: cfg.model_dir = a.model_dir
    if a.out_dir: cfg.out_dir = a.out_dir

    if a.cmd == "collect":
        from .collector import collect
        print(collect(cfg, days=a.days, symbols=a.symbols))
    elif a.cmd == "backfill":
        from .collector import backfill
        print(backfill(cfg, date.fromisoformat(a.start), date.fromisoformat(a.end) if a.end else None,
                       symbols=a.symbols, with_metrics=not a.no_metrics))
    elif a.cmd == "train":
        from .features import build_panel
        from .model import report_text, train
        from .storage import Store
        if a.horizon: cfg.horizon_h = a.horizon
        if a.pump: cfg.pump_pct = a.pump
        if a.dump: cfg.dump_pct = a.dump
        if a.test_frac: cfg.test_frac = a.test_frac
        store = Store(cfg.db_path)
        panel = build_panel(store, cfg)
        store.close()
        info = train(panel, cfg, importance=not a.no_importance)
        print(report_text(info, cfg.top_k))
    elif a.cmd == "score":
        from .scorer import score, to_markdown
        print(to_markdown(score(cfg, top=a.top, sort_by=a.sort, min_qv_usd=a.min_volume)))
    elif a.cmd == "events":
        from .events import pump_events
        from .features import build_panel
        from .storage import Store
        store = Store(cfg.db_path)
        ev = pump_events(build_panel(store, cfg), cfg)
        store.close()
        import os
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        ev.to_csv(a.out, index=False)
        print(f"{len(ev)} events -> {a.out}")


if __name__ == "__main__":
    main()
