"""Past pump events with the features at their start (for analysis / sanity checks).

An event starts at the first hour whose forward 72h high reaches +pump_pct; hours
within the same horizon after that are merged into one event."""
from __future__ import annotations

import pandas as pd

from .binance import HOUR_MS
from .config import Config

SHOW = ["ret_24h", "ret_7d", "oi_chg_24h", "funding_last", "vol_8h_ratio", "bbw_pct_30d",
        "past_pumps_60d", "f_oi_build_quiet", "f_mantra", "f_neg_funding", "f_quiet_vol_spike",
        "f_crash_oi", "f_hot_vol"]


def pump_events(panel: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    p = panel[panel.y_pump == 1].sort_values(["symbol", "t"])
    gap = p.groupby("symbol")["t"].diff()
    new = gap.isna() | (gap > cfg.horizon_h * HOUR_MS)
    ev = p[new].copy()
    ev["start_utc"] = pd.to_datetime(ev.t, unit="ms", utc=True)
    ev["any_precursor"] = ev[[c for c in SHOW if c.startswith("f_")]].max(axis=1)
    return ev[["symbol", "start_utc", "close", "fwd_max", "fwd_min", "y_pump_first", *SHOW, "any_precursor"]]
