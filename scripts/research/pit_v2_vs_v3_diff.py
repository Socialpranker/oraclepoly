#!/usr/bin/env python3
"""PIT v2 vs v3 metrics diff — Phase 1.C validation step.

Сравнивает stat metrics между pit_dataset_v2.parquet (9K rows, 5K markets)
и pit_dataset_v3.parquet (~80-100K rows, 50K markets).

Цель: убедиться, что v3 не drift'нул значения из-за расширения выборки.
Acceptance: mean values для key columns должны быть в пределах ±5% от v2.

Usage:
    python3 scripts/research/pit_v2_vs_v3_diff.py \
      [--v2 /mnt/data/polymarket_dataset/pit_dataset_legacy_v2.parquet] \
      [--v3 /mnt/data/polymarket_dataset/pit_dataset_v3.parquet] \
      [--out docs/research/2026-04-30-pit-v2-vs-v3-diff.md]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

KEY_COLUMNS = [
    # Oracle factors
    "market_price",
    "whale_raw",
    "maker_raw",
    "taker_raw",
    # Oracle scores
    "oracle_prod",
    "oracle_research",
    "div_prod",
    "div_research",
    # Win rates / outcomes
    "prod_win",
    "research_win",
    "winner_is_yes",
    # Volumes
    "total_volume_usd",
    "horizon_days",
]

# Acceptance: mean diff in absolute terms (proportion of v2 mean)
DIFF_THRESHOLD_PCT = 5.0  # ±5%


# ────────────────────────────────────────────────────────────────────────────
# Diff logic
# ────────────────────────────────────────────────────────────────────────────

def safe_diff_pct(v2_val: float, v3_val: float) -> float:
    """Relative diff in % between v2 and v3 means. Handles div-by-zero."""
    if abs(v2_val) < 1e-9:
        return 0.0 if abs(v3_val) < 1e-9 else float("inf")
    return 100.0 * (v3_val - v2_val) / v2_val


def compare_columns(df_v2: pd.DataFrame, df_v3: pd.DataFrame) -> list[dict]:
    """Per-column comparison: mean, median, std, count."""
    results = []
    for col in KEY_COLUMNS:
        if col not in df_v2.columns or col not in df_v3.columns:
            results.append({
                "column": col,
                "skip": f"missing in {'v2' if col not in df_v2.columns else 'v3'}",
            })
            continue

        v2_series = pd.to_numeric(df_v2[col], errors="coerce").dropna()
        v3_series = pd.to_numeric(df_v3[col], errors="coerce").dropna()

        v2_mean = float(v2_series.mean()) if len(v2_series) > 0 else 0.0
        v3_mean = float(v3_series.mean()) if len(v3_series) > 0 else 0.0
        v2_std = float(v2_series.std()) if len(v2_series) > 1 else 0.0
        v3_std = float(v3_series.std()) if len(v3_series) > 1 else 0.0

        diff_pct = safe_diff_pct(v2_mean, v3_mean)
        in_range = abs(diff_pct) <= DIFF_THRESHOLD_PCT

        results.append({
            "column": col,
            "v2_n": int(len(v2_series)),
            "v3_n": int(len(v3_series)),
            "v2_mean": round(v2_mean, 5),
            "v3_mean": round(v3_mean, 5),
            "diff_pct": round(diff_pct, 2),
            "v2_std": round(v2_std, 5),
            "v3_std": round(v3_std, 5),
            "in_range": in_range,
        })
    return results


def compare_categories(df_v2: pd.DataFrame, df_v3: pd.DataFrame) -> dict:
    """Sanity check: category coverage не пропал."""
    v2_cats = set(df_v2["category"].dropna().unique())
    v3_cats = set(df_v3["category"].dropna().unique())
    v2_top = df_v2["category"].value_counts().to_dict()
    v3_top = df_v3["category"].value_counts().to_dict()
    return {
        "v2_categories": sorted(v2_cats),
        "v3_categories": sorted(v3_cats),
        "missing_in_v3": sorted(v2_cats - v3_cats),
        "new_in_v3": sorted(v3_cats - v2_cats),
        "v2_top_3": dict(list(sorted(v2_top.items(), key=lambda x: -x[1]))[:3]),
        "v3_top_3": dict(list(sorted(v3_top.items(), key=lambda x: -x[1]))[:3]),
    }


def compare_phases(df_v2: pd.DataFrame, df_v3: pd.DataFrame) -> dict:
    """Sanity check: phase distribution ≈ 33/33/33%."""
    v2_p = df_v2["phase"].value_counts(normalize=True).round(3).to_dict()
    v3_p = df_v3["phase"].value_counts(normalize=True).round(3).to_dict()
    return {"v2": v2_p, "v3": v3_p}


# ────────────────────────────────────────────────────────────────────────────
# Markdown report
# ────────────────────────────────────────────────────────────────────────────

def render_report(
    v2_path: str, v3_path: str, df_v2: pd.DataFrame, df_v3: pd.DataFrame,
    col_results: list[dict], cat_results: dict, phase_results: dict,
) -> str:
    lines = []
    lines.append(f"# PIT v2 vs v3 Diff — {pd.Timestamp.now().date()}")
    lines.append("")
    lines.append(f"**v2:** `{v2_path}` ({len(df_v2):,} rows)")
    lines.append(f"**v3:** `{v3_path}` ({len(df_v3):,} rows)")
    lines.append(f"**Threshold:** ±{DIFF_THRESHOLD_PCT}% mean diff")
    lines.append("")

    # Verdict
    out_of_range = [r for r in col_results if "skip" not in r and not r["in_range"]]
    lines.append("## Verdict")
    lines.append("")
    if not out_of_range:
        lines.append("✅ **PASS** — все ключевые колонки в пределах ±5% диапазона.")
        lines.append("v3 dataset является валидным расширением v2 без statistical drift.")
    else:
        lines.append(f"⚠️ **WARN** — {len(out_of_range)} колонок outside ±5% range.")
        lines.append("Возможен drift из-за расширения выборки (новые markets, другие категории).")
        lines.append("Проверить: missing_in_v3 categories, sampling bias на --min-volume.")
    lines.append("")

    # Column-level diff
    lines.append("## Column Diff")
    lines.append("")
    lines.append("| Column | v2 n | v3 n | v2 mean | v3 mean | Diff % | In range? |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in col_results:
        if "skip" in r:
            lines.append(f"| {r['column']} | - | - | - | - | - | ❌ {r['skip']} |")
            continue
        emoji = "✅" if r["in_range"] else "⚠️"
        lines.append(
            f"| {r['column']} | {r['v2_n']:,} | {r['v3_n']:,} | "
            f"{r['v2_mean']} | {r['v3_mean']} | {r['diff_pct']:+.2f}% | {emoji} |"
        )
    lines.append("")

    # Category coverage
    lines.append("## Category Coverage")
    lines.append("")
    lines.append(f"- **v2 categories:** {', '.join(cat_results['v2_categories'])}")
    lines.append(f"- **v3 categories:** {', '.join(cat_results['v3_categories'])}")
    if cat_results["missing_in_v3"]:
        lines.append(f"- ⚠️ **Missing in v3:** {', '.join(cat_results['missing_in_v3'])}")
    if cat_results["new_in_v3"]:
        lines.append(f"- 🆕 **New in v3:** {', '.join(cat_results['new_in_v3'])}")
    lines.append(f"- **v2 top-3:** {cat_results['v2_top_3']}")
    lines.append(f"- **v3 top-3:** {cat_results['v3_top_3']}")
    lines.append("")

    # Phases
    lines.append("## Phase Distribution")
    lines.append("")
    lines.append(f"- **v2:** {phase_results['v2']}")
    lines.append(f"- **v3:** {phase_results['v3']}")
    lines.append("")

    # Stats
    lines.append("## Sample-Size Boost")
    lines.append("")
    boost = (len(df_v3) / len(df_v2)) if len(df_v2) > 0 else 0
    lines.append(f"- v3 / v2 = **{boost:.1f}x** rows")
    lines.append(f"- v2: {len(df_v2):,} rows")
    lines.append(f"- v3: {len(df_v3):,} rows")
    lines.append("")
    lines.append("**Implication:** DSR threshold в strategy_discovery падает с √(n_trials/N).")
    lines.append(f"При N→{len(df_v3):,} граница значимости опускается — реальные edges проявятся.")

    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--v2",
        default="/mnt/data/polymarket_dataset/pit_dataset_legacy_v2.parquet",
        help="Path к pit_dataset v2 parquet (legacy backup)",
    )
    parser.add_argument(
        "--v3",
        default="/mnt/data/polymarket_dataset/pit_dataset_v3.parquet",
        help="Path к pit_dataset v3 parquet (canonical)",
    )
    parser.add_argument(
        "--out",
        default="docs/research/2026-04-30-pit-v2-vs-v3-diff.md",
        help="Output markdown report",
    )
    args = parser.parse_args()

    if not os.path.exists(args.v2):
        # Fallback на pit_dataset_v2.parquet (если еще не переименован в legacy)
        alt_v2 = "/mnt/data/polymarket_dataset/pit_dataset_v2.parquet"
        if os.path.exists(alt_v2):
            args.v2 = alt_v2
            print(f"  v2 not at {args.v2}, fallback to {alt_v2}")
        else:
            print(f"ERROR: PIT v2 not found at {args.v2}")
            sys.exit(1)

    if not os.path.exists(args.v3):
        print(f"ERROR: PIT v3 not found at {args.v3}")
        print("  Phase 1.B production load должен быть завершён.")
        sys.exit(1)

    print(f"Loading v2: {args.v2}")
    df_v2 = pd.read_parquet(args.v2)
    print(f"  Rows: {len(df_v2):,}")

    print(f"Loading v3: {args.v3}")
    df_v3 = pd.read_parquet(args.v3)
    print(f"  Rows: {len(df_v3):,}")

    print("\nComparing columns...")
    col_results = compare_columns(df_v2, df_v3)

    print("Comparing categories...")
    cat_results = compare_categories(df_v2, df_v3)

    print("Comparing phases...")
    phase_results = compare_phases(df_v2, df_v3)

    # Render
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = render_report(args.v2, args.v3, df_v2, df_v3, col_results, cat_results, phase_results)
    out_path.write_text(report)

    out_of_range = [r for r in col_results if "skip" not in r and not r["in_range"]]
    print(f"\nReport: {out_path}")
    print(f"Sample-size boost: {len(df_v3) / max(len(df_v2), 1):.1f}x")
    if out_of_range:
        print(f"⚠️  {len(out_of_range)} columns outside ±5% range:")
        for r in out_of_range:
            print(f"   - {r['column']}: {r['v2_mean']} → {r['v3_mean']} ({r['diff_pct']:+.2f}%)")
        sys.exit(2)
    else:
        print("✅ All columns within ±5% range. v3 valid as canonical baseline.")


if __name__ == "__main__":
    main()
