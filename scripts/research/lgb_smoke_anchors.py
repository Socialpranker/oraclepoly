#!/usr/bin/env python3
"""LGB smoke per-anchor — проверяем не только anchor=24h.

Возможно V2 фичи дают edge на other anchors (4h, 72h, 168h).
"""

from __future__ import annotations

import sys

import duckdb
import lightgbm as lgb
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

ML_FEATURES = "/mnt/data/canonical/ml_features.parquet"


def smoke_for_anchor(con: duckdb.DuckDBPyConnection, anchor_h: int) -> tuple[float, float]:
    df = con.execute(f"""
        SELECT * FROM read_parquet('{ML_FEATURES}')
        WHERE anchor_hours = {anchor_h}
    """).df()

    if len(df) < 1000:
        return 0.0, 0.0

    df['_dt'] = pd.to_datetime(
        df['resolve_year'].astype(str) + '-' +
        df['resolve_month'].astype(str).str.zfill(2) + '-15'
    )
    train = df[df['_dt'] < '2025-10-01']
    test = df[df['_dt'] >= '2025-10-01']

    if len(train) < 1000 or len(test) < 1000:
        return 0.0, 0.0

    df['category'] = df['category'].astype('category')
    exclude = {'condition_id', 'event_id', 'yes_won', 'anchor_hours', '_dt'}
    feat_cols = [c for c in df.columns if c not in exclude]

    train_X = train[feat_cols].copy()
    test_X = test[feat_cols].copy()
    train_X['category'] = train_X['category'].astype('category')
    test_X['category'] = test_X['category'].astype('category')

    model = lgb.LGBMClassifier(
        n_estimators=200, learning_rate=0.05, max_depth=6,
        num_leaves=31, min_child_samples=100,
        objective='binary', verbose=-1
    )
    model.fit(train_X, train['yes_won'], categorical_feature=['category'])
    probs = model.predict_proba(test_X)[:, 1]
    auc = roc_auc_score(test['yes_won'], probs)
    brier = brier_score_loss(test['yes_won'], probs)
    return auc, brier


def main() -> None:
    print("LGB smoke per-anchor (V2, 55 cols)")
    print("=" * 60)

    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")

    anchors = [4, 24, 72, 168, 720]
    print(f"\n{'anchor':<10}{'AUC':<10}{'Brier':<10}")
    for h in anchors:
        auc, brier = smoke_for_anchor(con, h)
        print(f"{h:<10}{auc:.4f}    {brier:.4f}")


if __name__ == "__main__":
    sys.exit(main() or 0)
