#!/usr/bin/env python3
"""Train standalone LGB на ml_features V2 (55 cols) + per-event Brier vs active L2.

Цель: production-grade baseline LGB на чистых market features (без detector signals).
Сравнение с active L2 `lgb_l2_v20260502_053135` (brier 0.307 на detector_signals).

Methodology:
- anchor=24h primary (целевая зона торговли)
- time-based event-aware split (как honest_baseline)
- sample_weight = 1/event_size (per-event vote, не per-row)
- per-event Brier metric (apples-to-apples с honest_baseline 0.207)
- LightGBM с sensible defaults

Output:
  /mnt/data/canonical/models/standalone_lgb_v2_anchor24h_<ts>.pkl
  + JSON metadata

Запуск:
    cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/train_standalone_lgb_v2.py
"""

from __future__ import annotations

import json
import os
import pickle
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb
import lightgbm as lgb
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

ML_FEATURES = "/mnt/data/canonical/ml_features.parquet"
MODELS_DIR = Path("/mnt/data/canonical/models")

ANCHOR_HOURS = int(os.environ.get("ANCHOR_HOURS", "24"))
HOLDOUT_FRACTION = 0.20


def time_based_event_split(df: pd.DataFrame, holdout_fraction: float = 0.20) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Time-based split с event-aware boundary (как time_based_event_holdout_split)."""
    df = df.copy()
    df['_dt'] = pd.to_datetime(
        df['resolve_year'].astype(str) + '-' +
        df['resolve_month'].astype(str).str.zfill(2) + '-' +
        '15'
    )
    # event min ts
    event_min_ts = df.groupby('event_id')['_dt'].min().sort_values()
    cut = int(len(event_min_ts) * (1.0 - holdout_fraction))
    train_events = set(event_min_ts.index[:cut])
    holdout_events = set(event_min_ts.index[cut:])

    train = df[df['event_id'].isin(train_events)].copy()
    holdout = df[df['event_id'].isin(holdout_events)].copy()
    return train, holdout


def per_event_brier(df: pd.DataFrame, prob_col: str = 'prob') -> float:
    """Per-event Brier: усредняем prob/yes_won по event'у, считаем MSE напрямую.

    Для events где markets имеют смешанные outcomes (2/5 YES = 0.4) — это continuous target.
    brier_score_loss требует binary, поэтому используем MSE = E[(y - p)^2].
    """
    event_pred = df.groupby('event_id').agg(
        prob_mean=(prob_col, 'mean'),
        yes_won_mean=('yes_won', 'mean'),
    )
    return float(((event_pred['yes_won_mean'] - event_pred['prob_mean']) ** 2).mean())


def main() -> None:
    print("=" * 70)
    print("Standalone LGB V2 trainer (event-aware, sample-weight, per-event Brier)")
    print("=" * 70)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # Load
    print(f"\n[1/5] Loading ml_features.parquet anchor={ANCHOR_HOURS}h...")
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")
    df = con.execute(f"""
        SELECT * FROM read_parquet('{ML_FEATURES}')
        WHERE anchor_hours = {ANCHOR_HOURS}
    """).df()
    print(f"  rows: {len(df):,}  ({time.time()-t0:.1f}s)")

    # Event-aware time-based split
    print(f"\n[2/5] Event-aware time-based split (holdout {int(HOLDOUT_FRACTION*100)}%)...")
    train, holdout = time_based_event_split(df, HOLDOUT_FRACTION)
    print(f"  train: {len(train):,} rows / {train['event_id'].nunique():,} events")
    print(f"  holdout: {len(holdout):,} rows / {holdout['event_id'].nunique():,} events")

    # Sample weight = 1/event_size
    train_event_sizes = train.groupby('event_id').size()
    train['_weight'] = 1.0 / train['event_id'].map(train_event_sizes)
    holdout_event_sizes = holdout.groupby('event_id').size()
    holdout['_weight'] = 1.0 / holdout['event_id'].map(holdout_event_sizes)

    # Features
    df['category'] = df['category'].astype('category')
    exclude = {'condition_id', 'event_id', 'yes_won', 'anchor_hours', '_dt', '_weight'}
    feat_cols = [c for c in df.columns if c not in exclude]
    print(f"  features: {len(feat_cols)} cols")

    train_X = train[feat_cols].copy()
    holdout_X = holdout[feat_cols].copy()
    train_X['category'] = train_X['category'].astype('category')
    holdout_X['category'] = holdout_X['category'].astype('category')

    # Train
    print("\n[3/5] Training LightGBM...")
    t0 = time.time()
    model = lgb.LGBMClassifier(
        n_estimators=500,
        learning_rate=0.03,
        max_depth=7,
        num_leaves=63,
        min_child_samples=200,
        reg_alpha=0.1,
        reg_lambda=0.1,
        feature_fraction=0.8,
        objective='binary',
        verbose=-1,
        random_state=42,
    )
    model.fit(
        train_X, train['yes_won'],
        sample_weight=train['_weight'].values,
        categorical_feature=['category'],
    )
    train_time = time.time() - t0
    print(f"  train time: {train_time:.1f}s")

    # Eval
    print("\n[4/5] Evaluating on holdout...")
    train_probs = model.predict_proba(train_X)[:, 1]
    holdout_probs = model.predict_proba(holdout_X)[:, 1]

    # Per-row metrics (with sample_weight)
    train_brier_perrow = brier_score_loss(train['yes_won'], train_probs, sample_weight=train['_weight'])
    holdout_brier_perrow = brier_score_loss(holdout['yes_won'], holdout_probs, sample_weight=holdout['_weight'])
    holdout_auc = roc_auc_score(holdout['yes_won'], holdout_probs, sample_weight=holdout['_weight'])

    # Per-event metrics
    train['prob'] = train_probs
    holdout['prob'] = holdout_probs
    train_brier_perevent = per_event_brier(train)
    holdout_brier_perevent = per_event_brier(holdout)

    print(f"\n=== METRICS (anchor={ANCHOR_HOURS}h) ===")
    print("  TRAIN:")
    print(f"    per-row Brier (weighted):  {train_brier_perrow:.4f}")
    print(f"    per-event Brier:           {train_brier_perevent:.4f}")
    print("  HOLDOUT:")
    print(f"    per-row Brier (weighted):  {holdout_brier_perrow:.4f}")
    print(f"    per-event Brier:           {holdout_brier_perevent:.4f}")
    print(f"    per-row AUC (weighted):    {holdout_auc:.4f}")

    # Compare vs anchors
    print("\n=== COMPARISON ===")
    print("  honest_baseline _062541 (per-row):  0.222")
    print("  honest_baseline _073249 (per-row):  0.217")
    print("  honest_baseline _073249 (per-event): 0.207")
    print("  active L2 _053135 (per-row):        0.307  ★ active in production")
    print("  ─────────────────────────────────────────")
    print(f"  V2 standalone (per-row):            {holdout_brier_perrow:.4f}")
    print(f"  V2 standalone (per-event):          {holdout_brier_perevent:.4f}")

    # Verdict
    print("\n=== VERDICT ===")
    if holdout_brier_perevent < 0.207:
        print(f"  ✅ per-event Brier {holdout_brier_perevent:.4f} < honest_baseline 0.207 — competitive")
    elif holdout_brier_perevent < 0.222:
        print(f"  ✅ per-event Brier {holdout_brier_perevent:.4f} < _062541 (0.222)")
    else:
        print(f"  ⚠️  per-event Brier {holdout_brier_perevent:.4f} >= 0.222 — investigate")

    # Save
    print("\n[5/5] Saving model...")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    model_path = MODELS_DIR / f"standalone_lgb_v2_anchor{ANCHOR_HOURS}h_{ts}.pkl"
    meta_path = MODELS_DIR / f"standalone_lgb_v2_anchor{ANCHOR_HOURS}h_{ts}.json"

    with open(model_path, 'wb') as f:
        pickle.dump({
            'model': model,
            'feat_cols': feat_cols,
            'anchor_hours': ANCHOR_HOURS,
        }, f)

    feature_importance = sorted(
        zip(feat_cols, model.feature_importances_.tolist()),
        key=lambda x: x[1],
        reverse=True,
    )

    with open(meta_path, 'w') as f:
        json.dump({
            'version': f"standalone_lgb_v2_{ts}",
            'anchor_hours': ANCHOR_HOURS,
            'n_features': len(feat_cols),
            'n_train_rows': len(train),
            'n_holdout_rows': len(holdout),
            'n_train_events': train['event_id'].nunique(),
            'n_holdout_events': holdout['event_id'].nunique(),
            'train_brier_perrow_weighted': float(train_brier_perrow),
            'holdout_brier_perrow_weighted': float(holdout_brier_perrow),
            'train_brier_perevent': float(train_brier_perevent),
            'holdout_brier_perevent': float(holdout_brier_perevent),
            'holdout_auc_weighted': float(holdout_auc),
            'train_time_seconds': train_time,
            'top_25_features': feature_importance[:25],
            'split_method': 'time_based_event_aware',
            'holdout_fraction': HOLDOUT_FRACTION,
            'sample_weight': '1/event_size',
            'fitted_at': datetime.now().isoformat(),
        }, f, indent=2)
    print(f"  saved: {model_path.name}")
    print(f"  meta:  {meta_path.name}")


if __name__ == "__main__":
    sys.exit(main() or 0)
