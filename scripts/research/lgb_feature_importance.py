#!/usr/bin/env python3
"""LGB Feature Importance — find detector signals via tree learner.

Train LightGBM binary classifier (target=yes_won) on PIT-correct ml_features
per anchor. Extract top-20 features by importance (gain). For each top
feature: bucket WR + ROI breakdown to surface trading signal.

Goal: find dimensions/interactions that have predictive power but не
охвачены текущими D7b/D10b/D3 detectors.

Output:
  outputs/lgb_importance_TIMESTAMP_anchor{H}.csv  — feature ranking per anchor
  outputs/lgb_topfeature_buckets_TIMESTAMP.csv    — bucket WR/ROI per top feature
  Stdout: full breakdown
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd

ML_FEATURES = "/mnt/data/canonical/ml_features.parquet"
OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)

ANCHORS = [4, 24, 72]

# Categorical columns (LGB native handling)
CATEGORICAL_COLS = ["category"]

# Drop columns: leakage-prone or non-features
DROP_COLS = [
    "condition_id", "event_id", "anchor_hours", "yes_won",
    "yes_price_24h", "yes_price_72h", "yes_price_168h", "yes_price_720h",  # historical prices used to compute drift, redundant
    "resolve_year", "resolve_month",  # year/month могут leak season
]

# Honest haircut model
DEFAULT_FEE_PCT = 1.5
SLIPPAGE_PP = 2.5  # mid-band default


def load_features(con: duckdb.DuckDBPyConnection, anchor_h: int) -> pd.DataFrame:
    print(f"\n→ Loading features for anchor={anchor_h}h...")
    df = con.execute(
        f"""
        SELECT *
        FROM read_parquet('{ML_FEATURES}')
        WHERE anchor_hours = {anchor_h}
          AND yes_won IS NOT NULL
        """
    ).fetchdf()
    print(f"  rows: {len(df):,}")
    return df


def train_lgb(df: pd.DataFrame, anchor_h: int) -> tuple[lgb.Booster, list[str], pd.DataFrame]:
    """Train LGB on (features, yes_won). Returns model + feature names + importance df."""
    feat_cols = [c for c in df.columns if c not in DROP_COLS]
    y = df["yes_won"].astype(int).values

    # Encode category → category dtype для native LGB handling
    X = df[feat_cols].copy()
    for c in CATEGORICAL_COLS:
        if c in X.columns:
            X[c] = X[c].astype("category")

    # Chronological train/holdout split (по resolve_year/month если есть, иначе random)
    n = len(X)
    n_train = int(n * 0.7)
    X_train, y_train = X.iloc[:n_train], y[:n_train]
    X_test, y_test = X.iloc[n_train:], y[n_train:]

    print(f"  train n={n_train:,}, test n={n - n_train:,}")
    print(f"  train base WR: {y_train.mean()*100:.1f}%  test: {y_test.mean()*100:.1f}%")

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

    # Feature importance
    importance = pd.DataFrame({
        "feature": model.feature_name(),
        "importance_gain": model.feature_importance(importance_type="gain"),
        "importance_split": model.feature_importance(importance_type="split"),
    }).sort_values("importance_gain", ascending=False)

    # Predictions on test для validation
    preds = model.predict(X_test, num_iteration=model.best_iteration)
    auc = compute_auc(y_test, preds)
    brier = ((preds - y_test) ** 2).mean()
    print(f"  test AUC: {auc:.3f}  Brier: {brier:.4f}")

    return model, feat_cols, importance


def compute_auc(y_true, y_pred) -> float:
    """ROC AUC."""
    n = len(y_true)
    if n == 0:
        return 0.5
    pos_idx = y_true == 1
    n_pos = pos_idx.sum()
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    # Rank-based AUC
    order = np.argsort(y_pred)
    ranks = np.empty(n)
    ranks[order] = np.arange(1, n + 1)
    sum_ranks_pos = ranks[pos_idx].sum()
    auc = (sum_ranks_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return float(auc)


def feature_buckets(df: pd.DataFrame, feature: str, n_buckets: int = 5) -> list[dict]:
    """Quantile-bucket feature, compute WR/ROI per bucket."""
    if feature not in df.columns:
        return []
    series = df[feature].dropna()
    if series.dtype == object or series.dtype.name == "category":
        # categorical: groupby unique values
        unique_vals = df[feature].dropna().unique()
        if len(unique_vals) > 20:
            return []  # too many
        results = []
        for val in unique_vals:
            sub = df[df[feature] == val]
            if len(sub) < 200:
                continue
            results.extend(_compute_bucket_stats(sub, f"{feature}={val}"))
        return results

    # Numeric: quantile buckets
    try:
        qs = np.unique(np.quantile(series.values, np.linspace(0, 1, n_buckets + 1)))
    except Exception:
        return []
    if len(qs) < 3:
        return []

    results = []
    for i in range(len(qs) - 1):
        lo, hi = qs[i], qs[i + 1]
        if i == len(qs) - 2:
            sub = df[(df[feature] >= lo) & (df[feature] <= hi)]
        else:
            sub = df[(df[feature] >= lo) & (df[feature] < hi)]
        if len(sub) < 200:
            continue
        bucket_label = f"{feature}∈[{lo:.3f},{hi:.3f})"
        results.extend(_compute_bucket_stats(sub, bucket_label))

    return results


def _compute_bucket_stats(sub: pd.DataFrame, label: str) -> list[dict]:
    """For a sub-DataFrame, compute WR/ROI on both sides (NO and YES)."""
    n = len(sub)
    if n < 200:
        return []
    results = []
    for side in ["NO", "YES"]:
        # avg yes_price_at_anchor
        if "yes_price_at_anchor" not in sub.columns:
            continue
        avg_yes = sub["yes_price_at_anchor"].mean()
        if pd.isna(avg_yes) or avg_yes <= 0 or avg_yes >= 1:
            continue
        if side == "NO":
            wins = (sub["yes_won"] == 0).sum()
            cost = 1 - avg_yes
        else:
            wins = (sub["yes_won"] == 1).sum()
            cost = avg_yes
        wr = wins / n
        gross_roi = (wr * 1.0 - cost) / cost
        # net ROI
        cost_net = cost + SLIPPAGE_PP / 100
        net_roi = (wr * (1 - DEFAULT_FEE_PCT * 2 / 100) - cost_net) / cost_net
        results.append({
            "bucket": label,
            "side": side,
            "n": int(n),
            "wr": round(wr * 100, 2),
            "avg_cost": round(cost, 4),
            "gross_roi_pct": round(gross_roi * 100, 1),
            "net_roi_pct": round(net_roi * 100, 1),
        })
    return results


def main():
    print("=" * 70)
    print("LGB FEATURE IMPORTANCE — find new detector signals")
    print(f"Source: {ML_FEATURES}")
    print(f"Anchors: {ANCHORS}")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=4")

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    all_top_features: dict[int, pd.DataFrame] = {}
    all_buckets: list[dict] = []

    for anchor_h in ANCHORS:
        df = load_features(con, anchor_h)
        if len(df) < 1000:
            print("  skip — n too small")
            continue

        model, feat_cols, importance = train_lgb(df, anchor_h)
        all_top_features[anchor_h] = importance

        # Save importance CSV
        out_imp = OUT_DIR / f"lgb_importance_{ts}_anchor{anchor_h}.csv"
        importance.to_csv(out_imp, index=False)
        print(f"  → {out_imp}")

        # Print top 20
        print(f"\n  TOP 20 features by gain (anchor={anchor_h}h):")
        for _, row in importance.head(20).iterrows():
            print(f"    {row['feature']:<35} gain={row['importance_gain']:>12.0f}  splits={row['importance_split']:>5}")

        # Per-feature bucket analysis for top 10 numeric features
        print(f"\n  Bucket WR/ROI for top-10 features (anchor={anchor_h}h):")
        for _, row in importance.head(10).iterrows():
            feat = row["feature"]
            buckets = feature_buckets(df, feat)
            for b in buckets:
                b["anchor_h"] = anchor_h
                b["feature"] = feat
                b["importance_gain"] = row["importance_gain"]
                all_buckets.append(b)

    # Save consolidated buckets CSV
    if all_buckets:
        buckets_df = pd.DataFrame(all_buckets)
        # Strict filter: WR ≥ 60% OR net_roi ≥ 10% with n ≥ 500
        buckets_df["pass_strict"] = (
            (buckets_df["n"] >= 500) &
            (buckets_df["net_roi_pct"] >= 10)
        )
        buckets_df = buckets_df.sort_values("net_roi_pct", ascending=False)
        out_buckets = OUT_DIR / f"lgb_topfeature_buckets_{ts}.csv"
        buckets_df.to_csv(out_buckets, index=False)
        print(f"\n📊 Wrote {len(buckets_df)} buckets → {out_buckets}")

        # Print top-30 by net_roi (any sign + any n)
        print("\n" + "=" * 70)
        print("TOP-30 buckets by net_ROI (any n, any side)")
        print("=" * 70)
        top = buckets_df.head(30)
        for _, row in top.iterrows():
            star = "★" if row["pass_strict"] else " "
            print(
                f"{star} anchor={row['anchor_h']}h side={row['side']} | "
                f"{row['bucket']:<60} | "
                f"n={row['n']:>5} WR={row['wr']:>5.1f}% net_ROI={row['net_roi_pct']:+.1f}%"
            )

        # Strict only
        strict = buckets_df[buckets_df["pass_strict"]].head(20)
        if len(strict) > 0:
            print("\n" + "=" * 70)
            print("STRICT PASS cells (n≥500 AND net_ROI≥10%)")
            print("=" * 70)
            for _, row in strict.iterrows():
                print(
                    f"  anchor={row['anchor_h']}h {row['side']} | "
                    f"{row['bucket']:<55} | "
                    f"n={row['n']:>5} WR={row['wr']:>5.1f}% net_ROI={row['net_roi_pct']:+.1f}%"
                )
        else:
            print("\n⚠️  No strict-pass cells found.")

    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
