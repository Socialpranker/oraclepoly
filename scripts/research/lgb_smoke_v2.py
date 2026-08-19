#!/usr/bin/env python3
"""LGB smoke test для ml_features V2 (35 → 55 фич).

Сравнение:
  Baseline (35 cols): AUC 0.9295, Brier 0.0991 (anchor=24h, OOT test 2025-Q4+2026-Q1)
  V2 (55 cols):       ожидаем AUC > 0.94

Train/test split по resolve month (chronological walk-forward):
  train: < 2025-10-01
  test:  >= 2025-10-01
"""

from __future__ import annotations

import sys
import time

import duckdb
import lightgbm as lgb
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

ML_FEATURES = "/mnt/data/canonical/ml_features.parquet"


def main() -> None:
    print("LGB smoke test for ml_features V2 (35 → 55 cols)")
    print("=" * 60)

    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")

    print("\n[1/3] Loading anchor=24h ...")
    t0 = time.time()
    df = con.execute(f"""
        SELECT * FROM read_parquet('{ML_FEATURES}')
        WHERE anchor_hours = 24
    """).df()
    print(f"  rows: {len(df):,}  time: {time.time()-t0:.1f}s")

    # Walk-forward split
    df['_dt'] = pd.to_datetime(
        df['resolve_year'].astype(str) + '-' +
        df['resolve_month'].astype(str).str.zfill(2) + '-15'
    )
    train = df[df['_dt'] < '2025-10-01']
    test = df[df['_dt'] >= '2025-10-01']
    print(f"  train: {len(train):,}  test: {len(test):,}")
    print(f"  train base_yes: {train['yes_won'].mean():.4f}")
    print(f"  test  base_yes: {test['yes_won'].mean():.4f}")

    df['category'] = df['category'].astype('category')
    exclude = {'condition_id', 'event_id', 'yes_won', 'anchor_hours', '_dt'}
    feat_cols = [c for c in df.columns if c not in exclude]
    print(f"  feature columns: {len(feat_cols)}")

    train_X = train[feat_cols].copy()
    test_X = test[feat_cols].copy()
    train_X['category'] = train_X['category'].astype('category')
    test_X['category'] = test_X['category'].astype('category')

    print("\n[2/3] Training LightGBM (200 trees, lr=0.05) ...")
    t0 = time.time()
    model = lgb.LGBMClassifier(
        n_estimators=200, learning_rate=0.05, max_depth=6,
        num_leaves=31, min_child_samples=100,
        objective='binary', verbose=-1
    )
    model.fit(train_X, train['yes_won'], categorical_feature=['category'])
    print(f"  train time: {time.time()-t0:.1f}s")

    print("\n[3/3] Evaluation on out-of-time test ...")
    probs = model.predict_proba(test_X)[:, 1]
    auc = roc_auc_score(test['yes_won'], probs)
    brier = brier_score_loss(test['yes_won'], probs)

    print(f"\n=== METRICS ===")
    print(f"  AUC:   {auc:.4f}   (baseline V1: 0.9295)  Δ: {auc - 0.9295:+.4f}")
    print(f"  Brier: {brier:.4f}   (baseline V1: 0.0991)  Δ: {brier - 0.0991:+.4f}")

    # Top features
    print(f"\n=== TOP 25 FEATURES ===")
    imp = pd.DataFrame({
        'feat': feat_cols,
        'imp': model.feature_importances_
    }).sort_values('imp', ascending=False)

    new_features = {
        # Phase A wallet
        'n_unique_takers_24h', 'n_unique_makers_24h',
        'whale_taker_volume_24h', 'whale_taker_count_24h',
        'retail_taker_avg_size_24h', 'whale_buy_pressure_24h',
        'taker_hhi_proxy_24h', 'repeat_taker_ratio_24h',
        # Phase B microstructure
        'realized_vol_24h', 'realized_vol_72h', 'max_drawdown_24h_pct',
        'vpin_proxy_lifetime', 'kyle_lambda_proxy',
        'liquidity_quality_proxy', 'vol_24h_concentration',
        # Phase C event
        'event_total_lifetime_volume', 'event_thinnest_market_vol',
        'event_largest_market_share', 'event_yes_sum',
        'event_arb_distance_yes',
    }

    for i, row in enumerate(imp.head(25).itertuples(index=False), 1):
        marker = ' ★ NEW' if row.feat in new_features else ''
        print(f"  {i:>2}. {row.feat:<35}  {row.imp:>5}{marker}")

    # Importance breakdown by category
    new_imp_total = imp[imp['feat'].isin(new_features)]['imp'].sum()
    old_imp_total = imp[~imp['feat'].isin(new_features)]['imp'].sum()
    print(f"\n=== IMPORTANCE BREAKDOWN ===")
    print(f"  Old features (35): {old_imp_total:>5}  ({100*old_imp_total/(old_imp_total+new_imp_total):.1f}%)")
    print(f"  New features (20): {new_imp_total:>5}  ({100*new_imp_total/(old_imp_total+new_imp_total):.1f}%)")

    # Verdict
    print(f"\n=== VERDICT ===")
    if auc > 0.94:
        print(f"  ✅ AUC {auc:.4f} > 0.94 target — Phase A+B+C improvement CONFIRMED")
    elif auc > 0.93:
        print(f"  ⚠️  AUC {auc:.4f} > 0.93 baseline but < 0.94 target — partial improvement")
    else:
        print(f"  ❌ AUC {auc:.4f} <= baseline — investigate новые фичи")


if __name__ == "__main__":
    sys.exit(main() or 0)
