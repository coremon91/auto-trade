"""Training and evaluation.

* Rows: every symbol (delisted ones included) every `sample_every_h` hours, so the
  negatives are "ordinary times", not hand-picked calm periods.
* Split: by time only. Train = before the cutoff minus a `horizon_h` embargo
  (so no training label window overlaps the test period); test = after the cutoff.
* Three gradient-boosting models:
    p_pump        P(high >= +20% within 72h)
    p_dump        P(low  <= -15% within 72h)           (risk)
    p_pump_first  P(+20% is reached before -15%)       (what a long trade needs)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score, roc_auc_score

from .binance import HOUR_MS
from .config import Config
from .features import FEATURES

TARGETS = {"p_pump": "y_pump", "p_dump": "y_dump", "p_pump_first": "y_pump_first"}
RULES = ["f_oi_build_quiet", "f_mantra", "f_neg_funding", "f_quiet_vol_spike", "f_crash_oi", "f_hot_vol"]


def training_rows(panel: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    p = panel
    keep = (p["y_pump"].notna() & (p["age_days"] * 24 >= cfg.min_history_h)
            & ((p["t"] // HOUR_MS) % cfg.sample_every_h == 0))
    return p.loc[keep].reset_index(drop=True)


def time_split(rows: pd.DataFrame, cfg: Config):
    ts = np.sort(rows["t"].unique())
    if len(ts) < 10:
        raise ValueError("not enough history to split")
    cutoff = int(ts[int(len(ts) * (1 - cfg.test_frac))])
    embargo = cutoff - cfg.horizon_h * HOUR_MS
    train = rows[rows.t < embargo]
    test = rows[rows.t >= cutoff]
    return train, test, cutoff


def _model():
    return HistGradientBoostingClassifier(
        learning_rate=0.05, max_iter=400, max_leaf_nodes=31, min_samples_leaf=100,
        l2_regularization=1.0, early_stopping=True, validation_fraction=0.15,
        n_iter_no_change=30, random_state=7)


def precision_at_k(df: pd.DataFrame, score: str, label: str, k: int) -> float:
    top = df.sort_values(score, ascending=False).groupby("t").head(k)
    return float(top[label].mean()) if len(top) else float("nan")


def evaluate(test: pd.DataFrame, cfg: Config) -> dict:
    out = {}
    for col, y in TARGETS.items():
        yy = test[y].astype(int)
        m = {"base_rate": float(yy.mean()), "n": int(len(yy))}
        if yy.nunique() == 2:
            m["auc"] = float(roc_auc_score(yy, test[col]))
            m["avg_precision"] = float(average_precision_score(yy, test[col]))
        m[f"precision_at_{cfg.top_k}"] = precision_at_k(test, col, y, cfg.top_k)
        m["lift_at_k"] = (m[f"precision_at_{cfg.top_k}"] / m["base_rate"]) if m["base_rate"] else float("nan")
        out[col] = m
    # what happens to the pump-model top-k (direction risk)
    top = test.sort_values("p_pump", ascending=False).groupby("t").head(cfg.top_k)
    out["top_k_by_p_pump"] = {
        "hit_pump": float(top.y_pump.mean()), "hit_dump": float(top.y_dump.mean()),
        "pump_before_dump": float(top.y_pump_first.mean()),
        "median_fwd_max": float(top.fwd_max.median()), "median_fwd_min": float(top.fwd_min.median())}
    # rule flags from the study, as a baseline
    rules = {}
    for r in RULES:
        sel = test[test[r] == 1]
        rules[r] = {"n": int(len(sel)),
                    "hit_pump": float(sel.y_pump.mean()) if len(sel) else None,
                    "hit_dump": float(sel.y_dump.mean()) if len(sel) else None}
    out["rules"] = rules
    return out


def train(panel: pd.DataFrame, cfg: Config, importance: bool = True, log=print) -> dict:
    rows = training_rows(panel, cfg)
    train_df, test_df, cutoff = time_split(rows, cfg)
    log(f"rows: train {len(train_df):,}  test {len(test_df):,}  symbols {rows.symbol.nunique()}  "
        f"base P(pump) train {train_df.y_pump.mean():.3%} test {test_df.y_pump.mean():.3%}")
    X_tr, X_te = train_df[FEATURES], test_df[FEATURES]

    models = {}
    for col, y in TARGETS.items():
        yy = train_df[y].astype(int)
        if yy.nunique() < 2:
            raise ValueError(f"label {y} has a single class in training data")
        m = _model().fit(X_tr, yy)
        models[col] = m
        test_df = test_df.assign(**{col: m.predict_proba(X_te)[:, 1]})
    metrics = evaluate(test_df, cfg)

    imp = {}
    if importance and len(test_df) > 50 and test_df.y_pump.nunique() == 2:
        sub = test_df.sample(min(len(test_df), 20000), random_state=1)
        r = permutation_importance(models["p_pump"], sub[FEATURES], sub.y_pump.astype(int),
                                   scoring="roc_auc", n_repeats=3, random_state=1)
        imp = dict(sorted(zip(FEATURES, map(float, r.importances_mean)), key=lambda kv: -kv[1]))

    # final models are refit on all labelled rows so scoring uses the latest regime
    final = {col: _model().fit(rows[FEATURES], rows[y].astype(int)) for col, y in TARGETS.items()}

    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
    info = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "train_period": [fmt(int(train_df.t.min())), fmt(int(train_df.t.max()))],
        "test_period": [fmt(cutoff), fmt(int(test_df.t.max()))],
        "n_symbols": int(rows.symbol.nunique()),
        "label": {"horizon_h": cfg.horizon_h, "pump_pct": cfg.pump_pct, "dump_pct": cfg.dump_pct},
        "metrics": metrics,
        "importance_p_pump": imp,
    }
    os.makedirs(cfg.model_dir, exist_ok=True)
    joblib.dump({"models": final, "features": FEATURES, "info": info},
                os.path.join(cfg.model_dir, "screener.joblib"))
    with open(os.path.join(cfg.model_dir, "report.json"), "w") as fh:
        json.dump(info, fh, indent=2, ensure_ascii=False)
    test_df.to_csv(os.path.join(cfg.model_dir, "test_predictions.csv.gz"), index=False,
                   columns=["symbol", "t", "close", *TARGETS, *TARGETS.values(), "fwd_max", "fwd_min"])
    return info


def load(cfg: Config) -> dict:
    return joblib.load(os.path.join(cfg.model_dir, "screener.joblib"))


def report_text(info: dict, k: int = 10) -> str:
    m = info["metrics"]
    L = [f"학습 기간 {info['train_period'][0]} ~ {info['train_period'][1]} UTC, "
         f"검증 기간 {info['test_period'][0]} ~ {info['test_period'][1]} UTC, 종목 {info['n_symbols']}개",
         ""]
    names = {"p_pump": "급등(+%d%%)" % round(info['label']['pump_pct'] * 100),
             "p_dump": "급락(-%d%%)" % round(info['label']['dump_pct'] * 100),
             "p_pump_first": "급락 전 급등"}
    for col, nm in names.items():
        x = m[col]
        L.append(f"- {nm}: 기본확률 {x['base_rate']:.1%}, AUC {x.get('auc', float('nan')):.3f}, "
                 f"시점별 상위{k} 적중 {x[f'precision_at_{k}']:.1%} (리프트 {x['lift_at_k']:.1f}배)")
    t = m["top_k_by_p_pump"]
    L += ["", f"급등모델 상위{k}: 급등 {t['hit_pump']:.1%} / 급락 {t['hit_dump']:.1%} / "
              f"급락 전 급등 {t['pump_before_dump']:.1%}", "", "규칙 신호 (검증 기간):"]
    for r, v in m["rules"].items():
        if v["n"]:
            L.append(f"- {r}: {v['n']}건, 급등 {v['hit_pump']:.1%}, 급락 {v['hit_dump']:.1%}")
    if info.get("importance_p_pump"):
        top = list(info["importance_p_pump"].items())[:10]
        L += ["", "중요 특징 (permutation, AUC 감소): " + ", ".join(f"{a} {b:.3f}" for a, b in top)]
    return "\n".join(L)
