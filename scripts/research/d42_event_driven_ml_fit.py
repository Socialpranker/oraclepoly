"""D42 Event-driven ML — fit LGBM Regressor для price change prediction.

Гипотеза D3 (event-driven ML) из prediction_markets_full_landscape.docx:
  Не предсказываем outcome (yes_won), а **изменение цены за N часов**.
  "Самый прибыльный тип ML на prediction markets сейчас."

Architecture:
  Per-anchor models: predict `yes_price_at_target - yes_price_at_anchor`.

  Windows (from → to anchor):
    - 168h → 24h:  144h forward prediction
    -  72h → 24h:   48h forward
    -  24h →  4h:   20h forward

  Train на ml_features.parquet (55 cols, leak-free as_of anchor).
  Target derived из price_snapshots at_anchor + at_target hours_to_resolve.

Signal logic (predict-time):
  - predicted_delta > +5pp → YES (price will rise)
  - predicted_delta < -5pp → NO (price will fall)
  - |delta| < 5pp → no signal

Запуск:
    /root/oracle/.venv-research/bin/python scripts/research/d42_event_driven_ml_fit.py
    /root/oracle/.venv-research/bin/python scripts/research/d42_event_driven_ml_fit.py --window 168_to_24
"""

from __future__ import annotations

import argparse
import logging
import pickle
import sys
import time
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("d42_fit")

CANONICAL_DIR = Path("/mnt/data/canonical")
ML_FEATURES = CANONICAL_DIR / "ml_features.parquet"
SNAPSHOTS = CANONICAL_DIR / "price_snapshots.parquet"
MODELS_DIR = CANONICAL_DIR / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# Windows: (anchor_from_h, anchor_to_h) — train на anchor_from features, predict price@anchor_to
WINDOWS: dict[str, tuple[int, int]] = {
    "168_to_24": (168, 24),  # 144h forward, most signal
    "72_to_24": (72, 24),  # 48h forward
    "24_to_4": (24, 4),  # 20h forward
}

# Features used (excluding leak-prone + identity cols)
EXCLUDE_FEATURES = {
    "condition_id",
    "event_id",
    "anchor_hours",
    "yes_won",
    "_resolve_dt",
    "category",  # encoded separately
}

# Categorical: one-hot encoded
CATEGORICAL_COLS = ["category"]


def load_features_and_targets(window_name: str) -> pd.DataFrame:
    """Load ml_features at anchor_from + JOIN yes_price at anchor_to → derive delta target."""
    anchor_from, anchor_to = WINDOWS[window_name]
    con = duckdb.connect(":memory:")

    query = f"""
        SELECT
            f.*,
            s_to.yes_price                                     AS yes_price_at_target,
            f.yes_price_at_anchor                              AS yes_price_at_anchor_dup,
            s_to.yes_price - f.yes_price_at_anchor             AS delta_price
        FROM read_parquet('{ML_FEATURES}') f
        JOIN read_parquet('{SNAPSHOTS}') s_to
          ON s_to.condition_id = f.condition_id
         AND s_to.hours_to_resolve = {anchor_to}
        WHERE f.anchor_hours = {anchor_from}
          AND f.yes_price_at_anchor IS NOT NULL
          AND s_to.yes_price IS NOT NULL
    """
    df = con.execute(query).fetchdf()
    con.close()
    log.info("loaded window=%s n_rows=%d", window_name, len(df))
    return df


def prepare_xy(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """Split into X, y, return feature list."""
    y = df["delta_price"].astype(float)

    feat_cols = [
        c for c in df.columns
        if c not in EXCLUDE_FEATURES
        and c not in {"delta_price", "yes_price_at_target", "yes_price_at_anchor_dup"}
        and df[c].dtype.kind in ("i", "f", "b")
    ]

    X = df[feat_cols].copy()

    # Replace inf/nan with sentinel (LightGBM handles NaN natively, but inf needs care)
    X = X.replace([np.inf, -np.inf], np.nan)

    return X, y, feat_cols


def fit_model(X: pd.DataFrame, y: pd.Series) -> tuple[lgb.LGBMRegressor, dict]:
    """Fit LGBMRegressor + return holdout metrics.

    Walk-forward split: first 80% by row order = train, last 20% = holdout.
    ml_features.parquet sorted by _resolve_dt (oldest first), так что это
    time-respecting split.
    """
    n = len(X)
    split = int(n * 0.8)
    X_tr, X_ho = X.iloc[:split], X.iloc[split:]
    y_tr, y_ho = y.iloc[:split], y.iloc[split:]

    model = lgb.LGBMRegressor(
        n_estimators=200,
        learning_rate=0.05,
        max_depth=6,
        num_leaves=31,
        min_child_samples=50,
        reg_alpha=0.1,
        reg_lambda=0.1,
        random_state=42,
        verbose=-1,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_ho, y_ho)], callbacks=[lgb.early_stopping(20, verbose=False)])

    # Metrics
    y_pred_tr = model.predict(X_tr)
    y_pred_ho = model.predict(X_ho)

    rmse_tr = float(np.sqrt(((y_pred_tr - y_tr) ** 2).mean()))
    rmse_ho = float(np.sqrt(((y_pred_ho - y_ho) ** 2).mean()))

    # Sign accuracy (predicted vs actual direction)
    sign_acc_ho = float(((y_pred_ho > 0) == (y_ho > 0)).mean())

    # Bucket analysis: extreme predictions (>=5pp magnitude) should be more accurate
    big_pred = np.abs(y_pred_ho) >= 0.05
    sign_acc_big = float(((y_pred_ho[big_pred] > 0) == (y_ho[big_pred] > 0)).mean()) if big_pred.any() else 0.0
    n_big = int(big_pred.sum())

    metrics = {
        "n_train": int(split),
        "n_holdout": int(n - split),
        "rmse_train": rmse_tr,
        "rmse_holdout": rmse_ho,
        "sign_accuracy_holdout": sign_acc_ho,
        "n_big_signals_holdout": n_big,
        "sign_accuracy_big_holdout": sign_acc_big,
    }
    return model, metrics


def save_model(model: lgb.LGBMRegressor, feat_cols: list[str], window_name: str, metrics: dict) -> Path:
    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    path = MODELS_DIR / f"d42_event_driven_{window_name}_{timestamp}.pkl"
    with path.open("wb") as f:
        pickle.dump({"model": model, "feature_cols": feat_cols, "metrics": metrics, "window": window_name}, f)
    log.info("saved model=%s metrics=%s", path.name, metrics)
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", default="all", help=f"One of {list(WINDOWS.keys())} or 'all'")
    args = parser.parse_args()

    targets = [args.window] if args.window != "all" else list(WINDOWS.keys())

    for window_name in targets:
        if window_name not in WINDOWS:
            log.error("Unknown window: %s", window_name)
            sys.exit(2)

        log.info("══════════ %s ══════════", window_name)
        t0 = time.time()

        df = load_features_and_targets(window_name)
        if len(df) < 100:
            log.warning("Too few samples: %d, skipping", len(df))
            continue

        X, y, feat_cols = prepare_xy(df)
        log.info("X shape=%s, y range=[%.4f, %.4f]", X.shape, y.min(), y.max())

        model, metrics = fit_model(X, y)
        save_model(model, feat_cols, window_name, metrics)

        log.info("%s done in %.1fs", window_name, time.time() - t0)


if __name__ == "__main__":
    main()
