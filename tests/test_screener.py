import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from screener.binance import HOUR_MS, _read_csv_maybe_header, parse_klines, to_hourly_last
from screener.config import Config
from screener.features import FEATURES, build_panel, make_labels, symbol_frame
from screener.model import time_split, train, training_rows
from screener.storage import Store

from synthetic import T0, build_store, make_symbol


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=str(tmp_path / "m.sqlite"), model_dir=str(tmp_path / "models"),
                  out_dir=str(tmp_path / "out"))


def test_labels_forward_window_and_first_hit():
    cfg = Config(horizon_h=3, pump_pct=0.2, dump_pct=0.15)
    close = pd.Series([100, 100, 100, 100, 100, 100.0])
    high = pd.Series([100, 101, 125, 100, 100, 100.0])
    low = pd.Series([100, 80, 99, 99, 99, 99.0])
    lab = make_labels(high, low, close, cfg)
    # row 0 sees bars 1..3: dump at bar 1 before pump at bar 2
    assert lab.loc[0, "y_pump"] == 1 and lab.loc[0, "y_dump"] == 1 and lab.loc[0, "y_pump_first"] == 0
    assert lab.loc[0, "fwd_max"] == pytest.approx(0.25)
    # row 1 sees bars 2..4: pump only
    assert lab.loc[1, "y_pump"] == 1 and lab.loc[1, "y_dump"] == 0 and lab.loc[1, "y_pump_first"] == 1
    # the current bar itself is never part of its label
    assert lab.loc[2, "y_pump"] == 0
    # last H rows have no complete window
    assert lab.loc[3:, "y_pump"].isna().all()


def test_features_have_no_lookahead():
    cfg = Config()
    rng = np.random.default_rng(1)
    kl, oi, fr, ls, _ = make_symbol(rng, 1200, pump_every=300)
    a = symbol_frame(kl, oi, fr, ls, cfg)
    cut = 900
    t_cut = kl.ts.iloc[cut] + HOUR_MS
    # wreck everything after the cut, features up to the cut must not move
    kl2 = kl.copy(); kl2.loc[cut + 1:, ["open", "high", "low", "close"]] *= 3
    kl2.loc[cut + 1:, "quote_volume"] *= 50
    oi2 = oi.copy(); oi2.loc[oi2.ts > t_cut, "oi"] *= 5
    b = symbol_frame(kl2, oi2, fr, ls, cfg)
    cols = [c for c in FEATURES if c in a.columns]   # per-symbol features (cross-section added later)
    assert len(cols) > 30
    pd.testing.assert_frame_equal(a.loc[:cut, cols], b.loc[:cut, cols], check_dtype=False)


def test_asof_join_uses_value_known_at_bar_close():
    cfg = Config()
    rng = np.random.default_rng(2)
    kl, _, fr, ls, _ = make_symbol(rng, 200)
    oi = pd.DataFrame({"ts": kl.ts + HOUR_MS, "oi": np.arange(200.0) + 1, "oi_value": 1.0})
    f = symbol_frame(kl, oi, fr, ls, cfg)
    # row i closes at ts_i + 1h, which is exactly when OI snapshot i is stamped
    assert f.loc[30, "oi_chg_24h"] == pytest.approx(31 / 7 - 1)


def test_time_split_has_embargo(cfg):
    st, _ = build_store(cfg.db_path, n_symbols=6, n_hours=24 * 40)
    rows = training_rows(build_panel(st, cfg), cfg)
    tr, te, cutoff = time_split(rows, cfg)
    assert tr.t.max() < cutoff - cfg.horizon_h * HOUR_MS + 1
    assert te.t.min() >= cutoff


def test_storage_upsert_idempotent_and_keeps_delisted(cfg):
    st = Store(cfg.db_path)
    st.upsert_symbols([{"symbol": "AUSDT", "status": "TRADING"}, {"symbol": "BUSDT", "status": "TRADING"}])
    kl, *_ = make_symbol(np.random.default_rng(3), 50)
    assert st.upsert_klines("AUSDT", "1h", kl) == 50
    st.upsert_klines("AUSDT", "1h", kl)
    assert len(st.read("klines", "AUSDT")) == 50
    st.mark_missing_as_delisted({"AUSDT"})
    assert "BUSDT" in st.symbols(include_delisted=True)
    assert "BUSDT" not in st.symbols(include_delisted=False)


def test_long_short_merge_by_column(cfg):
    st = Store(cfg.db_path)
    st.upsert_long_short("X", pd.DataFrame({"ts": [1, 2], "global_ratio": [1.1, 1.2]}))
    st.upsert_long_short("X", pd.DataFrame({"ts": [1, 2], "taker_ratio": [0.9, 0.8]}))
    df = st.read("long_short", "X")
    assert df.global_ratio.tolist() == [1.1, 1.2] and df.taker_ratio.tolist() == [0.9, 0.8]


def test_end_to_end_train_and_score(cfg):
    st, names = build_store(cfg.db_path)
    panel = build_panel(st, cfg)
    st.close()
    assert "DEADUSDT" in set(panel.symbol)          # delisted symbol is in the training panel
    info = train(panel, cfg, importance=True, log=lambda *_: None)
    m = info["metrics"]["p_pump"]
    assert m["auc"] > 0.75                           # synthetic pumps have a learnable precursor
    assert m["lift_at_k"] > 1.1
    top_feats = list(info["importance_p_pump"])[:8]
    assert any(f.startswith("oi_chg") or f == "xs_oi_chg_24h" or f == "f_oi_build_quiet" for f in top_feats)

    from screener.scorer import score
    now = int(panel.t.max())
    wl = score(cfg, top=10, now_ms=now, min_qv_usd=0, log=lambda *_: None)
    assert 0 < len(wl) <= 10
    assert "DEADUSDT" not in set(wl.symbol)          # only listed symbols are scored
    assert wl.p_pump.is_monotonic_decreasing


def test_archive_csv_with_and_without_header():
    body = b"1700000000000,1,2,0.5,1.5,10,1700003599999,15,3,5,7,0\n"
    assert _read_csv_maybe_header(body).shape == (1, 12)
    hdr = (b"open_time,open,high,low,close,volume,close_time,quote_volume,count,"
           b"taker_buy_volume,taker_buy_quote_volume,ignore\n") + body
    df = _read_csv_maybe_header(hdr)
    k = parse_klines(df.iloc[:, :12].values.tolist())
    assert k.loc[0, "ts"] == 1700000000000 and k.loc[0, "close"] == 1.5


def test_to_hourly_last_keeps_real_timestamp():
    df = pd.DataFrame({"ts": [T0, T0 + 300_000, T0 + 3_300_000, T0 + HOUR_MS], "oi": [1, 2, 3, 4.0]})
    out = to_hourly_last(df, ["oi"])
    assert out.ts.tolist() == [T0 + 3_300_000, T0 + HOUR_MS]
    assert out.oi.tolist() == [3, 4]


def test_collect_with_fake_rest(cfg):
    from screener.collector import collect
    rng = np.random.default_rng(5)
    kl, oi, fr, ls, _ = make_symbol(rng, 100)

    class FakeREST:
        def perpetual_symbols(self, quote):
            return [{"symbol": "AUSDT", "status": "TRADING", "onboard_ms": T0}]
        def klines(self, s, i, start):
            return kl[kl.ts >= start]
        def open_interest_hist(self, s, p, start):
            return oi[oi.ts >= start]
        def funding(self, s, start):
            return fr[fr.ts >= start]
        def ratio_hist(self, s, kind, p, start):
            return ls[ls.ts >= start][["ts", kind]]

    s1 = collect(cfg, days=10000, rest=FakeREST(), log=lambda *_: None)
    s2 = collect(cfg, days=10000, rest=FakeREST(), log=lambda *_: None)   # second run resumes, adds nothing
    assert s1["klines"] == 100 and s2["klines"] == 0 and s1["errors"] == 0
    st = Store(cfg.db_path)
    assert st.read("long_short", "AUSDT").notna().all().all()
