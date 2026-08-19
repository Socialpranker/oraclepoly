"""Standalone V2 honest audit на canonical HF.

Цель: проверить cumulative $18,642 paper PnL на проде с честным slippage adjustment + DSR.

Метод:
1. Загрузить модели standalone_lgb_v2 для anchor 4h/24h/72h/168h/720h.
2. Inference на canonical ml_features.parquet HOLDOUT (последние 20% events по времени).
3. Per-(category, anchor) bucket: WR, ROI с slippage 0/3/5pp, DSR.
4. Decision: только buckets с PASS at 5pp + DSR p<0.05 → promote candidate.

Запуск:
    python3 scripts/research/standalone_v2_canonical_audit.py --out /tmp/standalone_audit.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pickle
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

CANONICAL = Path(os.environ.get("CANONICAL_DIR", "/mnt/data/canonical"))
MODELS_DIR = CANONICAL / "models"
ML_FEATURES = CANONICAL / "ml_features.parquet"
MARKETS = CANONICAL / "markets_clean.parquet"

ANCHORS = [4, 24, 72, 168, 720]
SLIPPAGES_PP = [0, 2, 3, 5, 8]
MIN_BUCKET_N = 30
MIN_EV_PCT = 5.0  # filter signals: |EV%| > 5%
DSR_TRIALS = 50  # 5 anchors × 10 categories


def load_model(anchor_h: int):
    pkl_files = sorted(MODELS_DIR.glob(f"standalone_lgb_v2_anchor{anchor_h}h_*.pkl"))
    if not pkl_files:
        return None, None, None
    pkl = pkl_files[-1]
    json_path = pkl.with_suffix(".json")
    with open(pkl, "rb") as f:
        bundle = pickle.load(f)
    with open(json_path) as f:
        meta = json.load(f)
    model = bundle["model"]
    feat_cols = bundle["feat_cols"]
    return model, meta, feat_cols


def deflated_sharpe_ratio(sharpe: float, n: int, n_trials: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """Bailey & Lopez de Prado deflation. Returns p-value (lower=more significant)."""
    if n < 30 or n_trials < 1:
        return 1.0
    e_max_sr = math.sqrt(2 * math.log(n_trials)) * (1 - 0.5772 / math.log(n_trials)) - 0.5
    sr_var = (1 - skew * sharpe + (kurt - 1) / 4 * sharpe**2) / (n - 1)
    if sr_var <= 0:
        return 1.0
    z = (sharpe - e_max_sr) / math.sqrt(sr_var)
    from scipy import stats
    return 1.0 - stats.norm.cdf(z)


def bucket_economics(ev_pct: np.ndarray, side: np.ndarray, yes_price: np.ndarray, yes_won: np.ndarray, slip_pp: float):
    """Compute WR, mean ROI, sharpe per bucket с slippage adjustment."""
    n = len(ev_pct)
    if n < MIN_BUCKET_N:
        return None
    rois = np.zeros(n)
    wins = np.zeros(n)
    for i in range(n):
        s = side[i]
        ypx = yes_price[i]
        yw = yes_won[i]
        slip = slip_pp / 100.0
        if s == "YES":
            entry = min(0.99, ypx + slip)
            won = (yw == 1)
        else:
            entry = min(0.99, (1.0 - ypx) + slip)
            won = (yw == 0)
        if entry < 0.005:
            entry = 0.005
        if won:
            rois[i] = (1.0 / entry) - 1.0
            wins[i] = 1
        else:
            rois[i] = -1.0
    mean_roi = float(rois.mean())
    wr = float(wins.mean())
    if rois.std() > 0:
        sharpe_per_trade = mean_roi / rois.std()
        sharpe_annualized = sharpe_per_trade * math.sqrt(min(252, n))
    else:
        sharpe_per_trade = 0.0
        sharpe_annualized = 0.0
    skew = float(((rois - mean_roi) ** 3).mean() / (rois.std() ** 3 + 1e-9))
    kurt = float(((rois - mean_roi) ** 4).mean() / (rois.std() ** 4 + 1e-9))
    dsr_p = deflated_sharpe_ratio(sharpe_per_trade, n, DSR_TRIALS, skew, kurt)
    return {
        "n": n,
        "wr_pct": wr * 100,
        "mean_roi_pct": mean_roi * 100,
        "sharpe_per_trade": sharpe_per_trade,
        "sharpe_annualized": sharpe_annualized,
        "dsr_p": dsr_p,
        "total_roi_per_dollar": float(rois.sum()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/standalone_audit.md")
    args = parser.parse_args()

    out_path = Path(args.out)
    lines = ["# Standalone V2 — Canonical Honest Audit\n"]
    lines.append("**Method:** holdout 20% time-based events. Slippage 0/2/3/5/8pp on entry.\n")
    lines.append(f"**Filter:** |EV%| > {MIN_EV_PCT}%. Bucket min n = {MIN_BUCKET_N}. DSR trials = {DSR_TRIALS}.\n\n")

    con = duckdb.connect()

    # Determine holdout cutoff per anchor — last 20% events by resolve time.
    # Using markets_clean.end_date as time axis (proxy для resolve order).

    for anchor_h in ANCHORS:
        model, meta, feat_cols = load_model(anchor_h)
        if model is None:
            print(f"No model for anchor {anchor_h}h, skipping")
            continue
        feat_names = feat_cols
        print(f"\nAnchor {anchor_h}h: {meta['version']}, holdout_brier={meta['holdout_brier_perrow_weighted']:.4f}")
        lines.append(f"\n## Anchor {anchor_h}h — {meta['version']}\n")
        lines.append(f"- Train rows: {meta['n_train_rows']:,}, Holdout rows: {meta['n_holdout_rows']:,}\n")
        lines.append(f"- Holdout Brier: {meta['holdout_brier_perrow_weighted']:.4f}, AUC: {meta['holdout_auc_weighted']:.4f}\n")

        # Get all features + outcome for holdout slice (last 20% events by event_id ordering by resolve time)
        # Use markets_clean.end_date to define cutoff event_ids
        cutoff_query = f"""
            WITH events AS (
                SELECT DISTINCT m.event_id, MAX(m.end_date) AS max_end
                FROM read_parquet('{ML_FEATURES}') f
                JOIN read_parquet('{MARKETS}') m ON f.condition_id = m.condition_id
                WHERE f.anchor_hours = {anchor_h}
                  AND f.yes_won IS NOT NULL
                  AND m.end_date IS NOT NULL
                GROUP BY m.event_id
            ),
            ranked AS (
                SELECT event_id, NTILE(5) OVER (ORDER BY max_end) AS quintile FROM events
            )
            SELECT event_id FROM ranked WHERE quintile = 5
        """
        holdout_event_ids = [r[0] for r in con.execute(cutoff_query).fetchall()]
        if not holdout_event_ids:
            print(f"  no holdout events for anchor {anchor_h}h")
            continue
        print(f"  holdout events: {len(holdout_event_ids):,}")

        # Load features for holdout
        sql = f"""
            SELECT f.*, m.category as cat_real
            FROM read_parquet('{ML_FEATURES}') f
            JOIN read_parquet('{MARKETS}') m ON f.condition_id = m.condition_id
            WHERE f.anchor_hours = {anchor_h}
              AND f.yes_won IS NOT NULL
              AND f.event_id IN ({','.join(f"'{e}'" for e in holdout_event_ids[:20000])})
        """
        df = con.execute(sql).df()
        if len(df) < 100:
            print(f"  too few rows: {len(df)}")
            continue
        print(f"  holdout rows loaded: {len(df):,}")

        # Build feature matrix matching model
        X_cols = [c for c in feat_names if c in df.columns]
        missing = [c for c in feat_names if c not in df.columns]
        if missing:
            print(f"  MISSING features: {missing[:5]}... skipping")
            continue
        X = df[X_cols].copy()
        # Cast all to numeric, NaN-safe
        for c in X.columns:
            X[c] = pd.to_numeric(X[c], errors="coerce")
        X = X.fillna(0.0)

        # LGBMClassifier.predict_proba returns [P(class=0), P(class=1)]; we want P(yes_won=1)
        proba = model.predict_proba(X.values)
        p_yes = proba[:, 1] if proba.ndim == 2 else proba
        market_yes = df["yes_price_at_anchor"].values
        yes_won = df["yes_won"].values.astype(int)
        category = df["cat_real"].fillna("Other").values

        # EV computation
        # YES bet: stake $1 → if win get 1/yes_price → ev = p_yes / yes_price - 1
        # NO bet:  stake $1 → if win get 1/(1-yes_price) → ev = (1-p_yes) / (1-yes_price) - 1
        ev_yes = (p_yes / np.maximum(market_yes, 0.005)) - 1.0
        ev_no = ((1.0 - p_yes) / np.maximum(1.0 - market_yes, 0.005)) - 1.0
        side = np.where(ev_yes >= ev_no, "YES", "NO")
        ev_pct = np.where(side == "YES", ev_yes * 100, ev_no * 100)

        # Filter: |EV%| > MIN_EV_PCT and reasonable price
        filt = (ev_pct > MIN_EV_PCT) & (market_yes >= 0.05) & (market_yes <= 0.95)
        idx = np.where(filt)[0]
        if len(idx) < MIN_BUCKET_N:
            print(f"  no signals after EV filter: {len(idx)}")
            continue

        # Per-category, slippage 0 / 3 / 5pp
        lines.append(f"- Signals after EV>{MIN_EV_PCT}% filter: {len(idx):,}\n\n")
        lines.append("| Category | n | Side mix | WR% | ROI@0pp | ROI@3pp | ROI@5pp | Sharpe | DSR p | Verdict |\n")
        lines.append("|---|---|---|---|---|---|---|---|---|---|\n")

        for cat in sorted(set(category[idx])):
            cat_idx = idx[category[idx] == cat]
            if len(cat_idx) < MIN_BUCKET_N:
                continue
            metrics = {}
            for slip in [0, 3, 5]:
                m = bucket_economics(ev_pct[cat_idx], side[cat_idx], market_yes[cat_idx], yes_won[cat_idx], slip)
                metrics[slip] = m
            base = metrics[0]
            slip5 = metrics[5]
            yes_count = int((side[cat_idx] == "YES").sum())
            no_count = int((side[cat_idx] == "NO").sum())
            side_mix = f"Y{yes_count}/N{no_count}"
            verdict_parts = []
            if slip5["mean_roi_pct"] > 5:
                verdict_parts.append("PASS@5pp")
            elif slip5["mean_roi_pct"] > 0:
                verdict_parts.append("MARGINAL@5pp")
            else:
                verdict_parts.append("KILL")
            if base["dsr_p"] < 0.05:
                verdict_parts.append("DSR✓")
            line = f"| {cat} | {base['n']} | {side_mix} | {base['wr_pct']:.1f}% | {base['mean_roi_pct']:+.1f}% | {metrics[3]['mean_roi_pct']:+.1f}% | {slip5['mean_roi_pct']:+.1f}% | {base['sharpe_per_trade']:.2f} | {base['dsr_p']:.3f} | {' + '.join(verdict_parts)} |\n"
            lines.append(line)
            print(f"  {cat:18s} n={base['n']:>5} WR={base['wr_pct']:.1f}% ROI@0={base['mean_roi_pct']:+.1f}% @3={metrics[3]['mean_roi_pct']:+.1f}% @5={slip5['mean_roi_pct']:+.1f}% DSR={base['dsr_p']:.3f}")

        # Daily projection for top buckets (slip5 PASS)
        passers = []
        for cat in sorted(set(category[idx])):
            cat_idx = idx[category[idx] == cat]
            if len(cat_idx) < MIN_BUCKET_N:
                continue
            m5 = bucket_economics(ev_pct[cat_idx], side[cat_idx], market_yes[cat_idx], yes_won[cat_idx], 5)
            if m5["mean_roi_pct"] > 5:
                passers.append((cat, m5))
        if passers:
            lines.append(f"\n**Passers (ROI@5pp > 5%):** {len(passers)} categories\n")
            for cat, m in passers:
                lines.append(f"- {cat}: n={m['n']}, ROI@5pp={m['mean_roi_pct']:+.1f}%, total_pnl_per_dollar={m['total_roi_per_dollar']:+.1f}\n")
        else:
            lines.append("\n**No passers at slippage 5pp.**\n")

    out_path.write_text("".join(lines))
    print(f"\n✅ Audit complete → {out_path}")


if __name__ == "__main__":
    main()
