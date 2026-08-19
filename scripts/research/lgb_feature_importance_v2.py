#!/usr/bin/env python3
"""LGB Feature Importance V2 — re-run на ml_features_v2 (с wallet quality cols).

Compared to V1 (на ml_features.parquet, 55 cols), V2 includes 8 wallet
quality features. Validates что wallet quality signals rank meaningful
high in tree learner gain importance.

Output:
  outputs/lgb_importance_v2_TIMESTAMP_anchor{H}.csv per anchor
  Stdout: top-20 features per anchor + comparison vs V1 (no wallet)
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd

ML_V2 = "/mnt/data/canonical/ml_features_v2.parquet"
OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)

ANCHORS = [4, 24, 72]
CATEGORICAL_COLS = ["category"]

DROP_COLS = [
    "condition_id", "event_id", "anchor_hours", "yes_won",
    "yes_price_24h", "yes_price_72h", "yes_price_168h", "yes_price_720h",
    "resolve_year", "resolve_month",
]

WALLET_QUALITY_COLS = {
    "n_takers_24h_before",
    "taker_volume_usd_total_24h",
    "smart_money_taker_volume_usd",
    "smart_money_taker_share",
    "top_decile_taker_volume_usd",
    "top_decile_taker_share",
    "taker_roi_weighted_24h",
    "taker_pnl_weighted_usd_24h",
}


def compute_auc(y_true, y_pred) -> float:
    n = len(y_true)
    pos_idx = y_true == 1
    n_pos = pos_idx.sum()
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(y_pred)
    ranks = np.empty(n)
    ranks[order] = np.arange(1, n + 1)
    sum_ranks_pos = ranks[pos_idx].sum()
    return float((sum_ranks_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def train_lgb(df: pd.DataFrame, anchor_h: int):
    feat_cols = [c for c in df.columns if c not in DROP_COLS]
    y = df["yes_won"].astype(int).values

    X = df[feat_cols].copy()
    for c in CATEGORICAL_COLS:
        if c in X.columns:
            X[c] = X[c].astype("category")

    n = len(X)
    n_train = int(n * 0.7)
    X_train, y_train = X.iloc[:n_train], y[:n_train]
    X_test, y_test = X.iloc[n_train:], y[n_train:]

    print(f"  train n={n_train:,}, test n={n - n_train:,}", flush=True)

    train_data = lgb.Dataset(X_train, label=y_train, categorical_feature=CATEGORICAL_COLS)
    test_data = lgb.Dataset(X_test, label=y_test, reference=train_data, categorical_feature=CATEGORICAL_COLS)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "learning_rate": 0.05,
        "num_leaves": 63,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "min_data_in_leaf": 100,
        "verbose": -1,
    }

    model = lgb.train(
        params,
        train_data,
        num_boost_round=300,
        valid_sets=[test_data],
        callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)],
    )

    importance = pd.DataFrame({
        "feature": model.feature_name(),
        "importance_gain": model.feature_importance(importance_type="gain"),
        "importance_split": model.feature_importance(importance_type="split"),
    }).sort_values("importance_gain", ascending=False)

    preds = model.predict(X_test, num_iteration=model.best_iteration)
    auc = compute_auc(y_test, preds)
    brier = ((preds - y_test) ** 2).mean()
    print(f"  test AUC: {auc:.3f}  Brier: {brier:.4f}", flush=True)

    return importance, auc, brier


def main():
    print("=" * 70, flush=True)
    print("LGB Feature Importance V2 — ml_features_v2 (with wallet quality)", flush=True)
    print("=" * 70, flush=True)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='5GB'")
    con.execute("SET threads=4")

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    for anchor_h in ANCHORS:
        print(f"\n{'='*70}\nAnchor: {anchor_h}h\n{'='*70}", flush=True)
        df = con.execute(
            f"""
            SELECT *
            FROM read_parquet('{ML_V2}')
            WHERE anchor_hours = {anchor_h}
              AND yes_won IS NOT NULL
            """
        ).fetchdf()
        print(f"rows: {len(df):,}", flush=True)
        if len(df) < 1000:
            continue

        importance, auc, brier = train_lgb(df, anchor_h)

        out = OUT_DIR / f"lgb_importance_v2_{ts}_anchor{anchor_h}.csv"
        importance.to_csv(out, index=False)

        print(f"\nTOP 20 features by gain (anchor={anchor_h}h):", flush=True)
        for i, row in enumerate(importance.head(20).itertuples(), 1):
            wallet_flag = "[WALLET]" if row.feature in WALLET_QUALITY_COLS else "        "
            print(f"  {i:>2}. {wallet_flag} {row.feature:<35} gain={row.importance_gain:>14.0f} splits={row.importance_split:>5}", flush=True)

        # Specifically: где ranked wallet quality features
        wallet_ranks = []
        for i, row in enumerate(importance.itertuples(), 1):
            if row.feature in WALLET_QUALITY_COLS:
                wallet_ranks.append((i, row.feature, row.importance_gain))

        print(f"\nWallet quality features rank summary:", flush=True)
        for rank, feat, gain in wallet_ranks:
            print(f"  rank {rank:>3}: {feat:<35} gain={gain:>14.0f}", flush=True)

    print(f"\nDone in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
