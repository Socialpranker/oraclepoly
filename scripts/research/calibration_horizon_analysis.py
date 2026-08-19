#!/usr/bin/env python3
"""Calibration v3 hypothesis test — стоит ли делать per-(category, horizon_bin) weights.

Phase 2.A roadmap step (см. docs/superpowers/plans/2026-04-29-dataset/03-phase-2-calibration-v3.md).

Гипотеза:
    Текущая калибровка per-category global average. Если веса различаются по horizon
    (Sports h_short — maker_direction доминирует; Politics h_long — whale+news),
    то per-(cat, horizon) weights дают улучшение Brier score.

Логика:
    1. Загрузить pit_dataset_v3.parquet (или v2 для smoke test).
    2. Для каждой (category, horizon_bin) ячейки с n ≥ MIN_SAMPLES:
       - Считать Brier с current default_weights (baseline)
       - Optuna поиск optimal weights на subset (50 trials)
       - Считать Brier с optimal_weights
       - improvement = (brier_default - brier_opt) / brier_default
    3. Hot candidates: improvement > 5% AND n ≥ 1000.

Verdict:
    - Если 0 hot candidates → Phase 2 ABORT (weight space плоский).
    - Если ≥3 hot candidates → proceed to migration + code changes.

Usage:
    python3 scripts/research/calibration_horizon_analysis.py \
      --pit /mnt/data/polymarket_dataset/pit_dataset_v3.parquet \
      --out docs/research/2026-04-30-calibration-horizon-feasibility.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

CATEGORIES = ["Sports", "Politics", "Crypto", "Tech & AI", "World & Economy", "Culture"]

HORIZON_BINS = [
    ("<3d",     0,    3),
    ("3-7d",    3,    7),
    ("7-14d",   7,   14),
    ("14-30d", 14,   30),
    (">30d",   30, 1000),  # cap 1000 to handle outliers
]

MIN_SAMPLES_PER_BIN = 200       # Skip cells smaller than this
HOT_CANDIDATE_MIN_N = 1000      # Promotion threshold
HOT_IMPROVEMENT_THRESHOLD = 0.05  # 5% Brier improvement


# ────────────────────────────────────────────────────────────────────────────
# Brier score
# ────────────────────────────────────────────────────────────────────────────

def brier_score(predictions: np.ndarray, outcomes: np.ndarray) -> float:
    """Brier = (1/N) * Σ (p - y)²."""
    return float(np.mean((predictions - outcomes) ** 2))


# ────────────────────────────────────────────────────────────────────────────
# Per-cell analysis
# ────────────────────────────────────────────────────────────────────────────

def analyse_cell(
    df_cell: pd.DataFrame,
    n_optuna_trials: int = 50,
) -> dict:
    """Для одной (cat, horizon_bin) ячейки сравнить default vs optimized weights.

    Returns dict с metrics.
    """
    from optuna import create_study
    from optuna.samplers import TPESampler

    n = len(df_cell)
    if n < MIN_SAMPLES_PER_BIN:
        return {"n": n, "skip_reason": f"n<{MIN_SAMPLES_PER_BIN}"}

    # Outcome: использовать `winner_is_yes` если есть, иначе fallback на prod_win
    if "winner_is_yes" in df_cell.columns:
        y = df_cell["winner_is_yes"].astype(int).values
    else:
        y = df_cell["prod_win"].astype(int).values

    # Baseline: prod oracle_score (уже включает default_weights)
    p_baseline = df_cell["oracle_prod"].clip(0.01, 0.99).values
    brier_default = brier_score(p_baseline, y)

    # Optuna: ищем optimal scaling factor для текущего oracle output
    # (Не пере-вычисляем weights с нуля — это требовало бы исходные factors,
    # которые в pit_v3 уже agregated. Делаем sigmoid recalibration с (a, b))
    def objective(trial):
        a = trial.suggest_float("a", 0.5, 2.5)
        b = trial.suggest_float("b", -1.0, 1.0)
        # logit recalibration: p_new = sigmoid(a * logit(p) + b)
        logits = np.log(p_baseline / (1 - p_baseline))
        p_new = 1.0 / (1.0 + np.exp(-(a * logits + b)))
        return brier_score(p_new, y)

    study = create_study(direction="minimize", sampler=TPESampler(seed=42))
    # Enqueue identity (a=1, b=0) — гарантирует что optimization >= default
    study.enqueue_trial({"a": 1.0, "b": 0.0})
    study.optimize(objective, n_trials=n_optuna_trials, show_progress_bar=False)
    brier_opt = study.best_value
    best_a, best_b = study.best_params["a"], study.best_params["b"]

    improvement = (brier_default - brier_opt) / brier_default if brier_default > 0 else 0.0

    return {
        "n": n,
        "brier_default": round(brier_default, 5),
        "brier_optimized": round(brier_opt, 5),
        "improvement_pct": round(100 * improvement, 2),
        "best_a": round(best_a, 4),
        "best_b": round(best_b, 4),
        "is_hot": improvement >= HOT_IMPROVEMENT_THRESHOLD and n >= HOT_CANDIDATE_MIN_N,
    }


def assign_horizon_bin(horizon_days: float) -> str | None:
    """Map float horizon → bin label."""
    for label, lo, hi in HORIZON_BINS:
        if lo <= horizon_days < hi:
            return label
    return None


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--pit",
        default="/mnt/data/polymarket_dataset/pit_dataset_v3.parquet",
        help="Path к PIT dataset parquet",
    )
    parser.add_argument(
        "--out",
        default="docs/research/2026-04-30-calibration-horizon-feasibility.md",
        help="Output markdown report",
    )
    parser.add_argument(
        "--n-trials",
        type=int,
        default=50,
        help="Optuna trials per cell",
    )
    args = parser.parse_args()

    if not os.path.exists(args.pit):
        print(f"ERROR: PIT dataset not found: {args.pit}")
        print(f"  Phase 1 должен быть завершён до запуска Phase 2.A.")
        sys.exit(1)

    print(f"Loading {args.pit}...")
    df = pd.read_parquet(args.pit)
    print(f"  Rows: {len(df):,}, Cols: {len(df.columns)}")

    # Map horizon_days → horizon_bin
    df["horizon_bin"] = df["horizon_days"].apply(assign_horizon_bin)
    df = df[df["horizon_bin"].notna()].copy()
    print(f"  After horizon filter: {len(df):,} rows")

    # Iterate cells
    results = []
    print(f"\nAnalyzing cells (n_trials={args.n_trials})...")
    for cat in CATEGORIES:
        for hb_label, _, _ in HORIZON_BINS:
            df_cell = df[(df["category"] == cat) & (df["horizon_bin"] == hb_label)].copy()
            n = len(df_cell)
            print(f"  [{cat:>16s}] [{hb_label:>6s}] n={n:>5d}", end=" ", flush=True)
            metrics = analyse_cell(df_cell, n_optuna_trials=args.n_trials)
            metrics["category"] = cat
            metrics["horizon_bin"] = hb_label
            results.append(metrics)
            if "skip_reason" in metrics:
                print(f"SKIP ({metrics['skip_reason']})")
            else:
                hot_marker = "🔥 HOT" if metrics["is_hot"] else ""
                print(
                    f"brier {metrics['brier_default']:.4f} → "
                    f"{metrics['brier_optimized']:.4f} "
                    f"({metrics['improvement_pct']:+.2f}%) {hot_marker}"
                )

    hot_candidates = [r for r in results if r.get("is_hot")]
    valid = [r for r in results if "skip_reason" not in r]

    # Build report
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append(f"# Calibration v3 Horizon Feasibility — {pd.Timestamp.now().date()}")
    lines.append("")
    lines.append(f"**Источник:** `{args.pit}` ({len(df):,} rows)")
    lines.append(f"**Скрипт:** `scripts/research/calibration_horizon_analysis.py`")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    if len(hot_candidates) == 0:
        lines.append("❌ **PHASE 2 ABORT** — 0 ячеек с improvement ≥5% и n≥1000.")
        lines.append("Per-horizon segmentation **не окупается** на текущих данных.")
    elif len(hot_candidates) >= 3:
        lines.append(f"✅ **PROCEED** — {len(hot_candidates)} hot candidates.")
        lines.append("Per-horizon segmentation окупается. Готовиться к migration + code changes.")
    else:
        lines.append(f"⚠️ **BORDERLINE** — {len(hot_candidates)} hot candidates (нужно ≥3).")
        lines.append("Можно подождать больше данных или применить только к hot ячейкам.")
    lines.append("")
    lines.append("## Hot Candidates")
    lines.append("")
    lines.append("| Category | Horizon | n | Brier default | Brier opt | Improvement | a | b |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in sorted(hot_candidates, key=lambda x: -x["improvement_pct"]):
        lines.append(
            f"| {r['category']} | {r['horizon_bin']} | {r['n']:,} | "
            f"{r['brier_default']:.4f} | {r['brier_optimized']:.4f} | "
            f"**{r['improvement_pct']:+.2f}%** | {r['best_a']} | {r['best_b']} |"
        )
    lines.append("")
    lines.append("## Full Results")
    lines.append("")
    lines.append("| Category | Horizon | n | Brier default | Brier opt | Improvement | Hot? |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in valid:
        lines.append(
            f"| {r['category']} | {r['horizon_bin']} | {r['n']:,} | "
            f"{r['brier_default']:.4f} | {r['brier_optimized']:.4f} | "
            f"{r['improvement_pct']:+.2f}% | {'✅' if r['is_hot'] else ''} |"
        )
    skipped = [r for r in results if "skip_reason" in r]
    if skipped:
        lines.append("")
        lines.append(f"## Skipped Cells ({len(skipped)})")
        lines.append("")
        lines.append("| Category | Horizon | n | Reason |")
        lines.append("|---|---|---|---|")
        for r in skipped:
            lines.append(f"| {r['category']} | {r['horizon_bin']} | {r['n']} | {r['skip_reason']} |")

    out_path.write_text("\n".join(lines))
    print(f"\nReport: {out_path}")
    print(f"Hot candidates: {len(hot_candidates)} / {len(valid)}")


if __name__ == "__main__":
    main()
