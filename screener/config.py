"""Screener settings. Every value can be overridden from the CLI."""
from dataclasses import dataclass, field
import os


@dataclass
class Config:
    # Storage
    db_path: str = os.environ.get("SCREENER_DB", "data/market.sqlite")
    model_dir: str = os.environ.get("SCREENER_MODEL_DIR", "models")
    out_dir: str = os.environ.get("SCREENER_OUT_DIR", "output")

    # Binance endpoints (public, no API key)
    fapi_base: str = os.environ.get("BINANCE_FAPI", "https://fapi.binance.com")
    archive_base: str = os.environ.get("BINANCE_ARCHIVE", "https://data.binance.vision")
    request_sleep: float = 0.12  # seconds between REST calls (weight-friendly)

    # Data grid
    base_interval: str = "1h"     # all features are built on a 1h grid
    quote_asset: str = "USDT"

    # Label definition
    horizon_h: int = 72           # look-ahead window
    pump_pct: float = 0.20        # +20% (high vs. entry close) within horizon
    big_pump_pct: float = 0.30    # +30% secondary label
    dump_pct: float = 0.15        # -15% risk label (low vs. entry close)

    # Training
    sample_every_h: int = 4       # one training row per symbol every N hours
    min_history_h: int = 24 * 7   # skip a symbol's first week (features unstable)
    test_frac: float = 0.25       # last 25% of time is the test period
    top_k: int = 10               # precision@k per timestamp

    # Rule thresholds (from the 2026-11 precursor study)
    oi_build_pct: float = 0.10        # OI +10% in 24h
    neg_funding: float = -0.0003      # funding <= -0.03%
    quiet_vol_mult: float = 3.0       # 8h volume >= 3x normal
    quiet_vol_price: float = 0.05     # ... while |price move| < 5%

    extra: dict = field(default_factory=dict)


DEFAULT = Config()
