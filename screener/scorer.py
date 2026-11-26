"""Score the latest closed hour for every active symbol and write a ranked watchlist."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pandas as pd

from .binance import HOUR_MS
from .config import Config
from .features import build_panel
from .model import TARGETS, load
from .storage import Store

FLAG_KO = {"f_oi_build_quiet": "가격횡보+OI증가", "f_mantra": "하락후 OI급증(MANTRA형)",
           "f_neg_funding": "음수펀딩", "f_quiet_vol_spike": "조용한 거래량급증",
           "f_crash_oi": "급락+OI급증(고위험)", "f_hot_vol": "고변동+7일상승 상위"}


def score(cfg: Config, top: int = 30, sort_by: str = "p_pump", min_qv_usd: float = 5e6,
          max_stale_h: int = 3, now_ms: int | None = None, log=print) -> pd.DataFrame:
    bundle = load(cfg)
    store = Store(cfg.db_path)
    now_ms = now_ms or int(time.time() * 1000)
    active = store.symbols(include_delisted=False)
    panel = build_panel(store, cfg, symbols=active, since_ms=now_ms - 3 * 24 * HOUR_MS, log=log)
    store.close()
    if panel.empty:
        raise RuntimeError("no recent data; run `collect` first")
    last = panel.sort_values("t").groupby("symbol").tail(1)
    last = last[last.t >= now_ms - max_stale_h * HOUR_MS]          # drop stale / halted symbols
    last = last[10 ** last.log_qv_24h.fillna(0) >= min_qv_usd]       # liquidity filter
    X = last[bundle["features"]]
    for col, m in bundle["models"].items():
        last[col] = m.predict_proba(X)[:, 1]
    last["flags"] = last.apply(lambda r: ", ".join(v for k, v in FLAG_KO.items() if r.get(k) == 1), axis=1)
    cols = ["symbol", "close", *TARGETS, "ret_24h", "ret_7d", "oi_chg_24h", "funding_last",
            "vol_8h_ratio", "bbw_80h", "past_pumps_60d", "flags"]
    out = last.sort_values(sort_by, ascending=False)[cols].head(top).reset_index(drop=True)
    out.index += 1
    asof = datetime.fromtimestamp(int(last.t.max()) / 1000, timezone.utc)
    out.attrs["asof"] = asof.strftime("%Y-%m-%d %H:%M UTC")
    os.makedirs(cfg.out_dir, exist_ok=True)
    stamp = asof.strftime("%Y%m%d_%H%M")
    out.to_csv(os.path.join(cfg.out_dir, f"watchlist_{stamp}.csv"), index_label="rank")
    with open(os.path.join(cfg.out_dir, "watchlist_latest.md"), "w") as fh:
        fh.write(to_markdown(out))
    return out


def to_markdown(df: pd.DataFrame) -> str:
    L = [f"# 급등 후보 워치리스트 ({df.attrs.get('asof', '')})", "",
         "| # | 종목 | 급등확률 | 급락확률 | 급락전급등 | 24h | 7d | OI 24h | 펀딩 | 거래량8h | 신호 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    pc = lambda v: "" if pd.isna(v) else f"{v:+.1%}"
    for i, r in df.iterrows():
        L.append(f"| {i} | {r.symbol} | {r.p_pump:.1%} | {r.p_dump:.1%} | {r.p_pump_first:.1%} | "
                 f"{pc(r.ret_24h)} | {pc(r.ret_7d)} | {pc(r.oi_chg_24h)} | "
                 f"{'' if pd.isna(r.funding_last) else f'{r.funding_last:.3%}'} | "
                 f"{'' if pd.isna(r.vol_8h_ratio) else f'{r.vol_8h_ratio:.1f}x'} | {r.flags} |")
    L += ["", "급등확률 = 72시간 안에 +20% 도달 확률, 급락확률 = 같은 기간 -15% 도달 확률, "
              "급락전급등 = -15%보다 +20%를 먼저 찍을 확률. 진입 신호가 아니라 관찰 대상 목록입니다."]
    return "\n".join(L) + "\n"
