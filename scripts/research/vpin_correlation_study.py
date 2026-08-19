#!/usr/bin/env python3
"""VPIN correlation study — Phase 5.1 (independent от pit_v3).

Проверяет гипотезу: VPIN > p95 → информированная торговля → market price движется
к truth → VPIN коррелирует с outcome accuracy.

Логика:
1. Читает quant.parquet + markets.parquet (для outcomes)
2. Для каждого resolved market с volume ≥ $10K и ≥100 trades:
   - Берёт trades за окно [resolve_time - 24h, resolve_time]
   - Считает VPIN classical (Easley/López de Prado/O'Hara)
3. Корреляция: VPIN vs |market_price_at_resolve - outcome|
   - High |corr| → VPIN предсказывает где price точнее
   - Low |corr| → VPIN не несёт информации поверх market_price

Verdict для Phase 5.1:
- |corr| ≥ 0.10 → integrate VPIN as factor (w_vpin = 0.15)
- |corr| < 0.05 → reject, document negative result
- 0.05-0.10 → borderline, investigate per-category

Usage:
    python3 scripts/research/vpin_correlation_study.py \\
      --quant /mnt/data/hf_data/quant.parquet \\
      --markets /mnt/data/polymarket_dataset/markets.parquet \\
      --out docs/research/2026-04-30-vpin-correlation.md \\
      [--max-markets 5000]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.core.scoring import BayesianScoreCalculator  # noqa: E402

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

VPIN_WINDOW_SEC = 24 * 3600   # 24h до resolve
MIN_TRADES_PER_MARKET = 100
MIN_VOLUME_USD = 10_000.0
VPIN_BUCKETS = 10

# Acceptance criteria
INTEGRATE_THRESHOLD = 0.10
REJECT_THRESHOLD = 0.05


# ────────────────────────────────────────────────────────────────────────────
# Per-market VPIN
# ────────────────────────────────────────────────────────────────────────────

def compute_market_vpin(trades_df: pd.DataFrame) -> float | None:
    """VPIN classical algorithm per market.

    trades_df: DataFrame с (timestamp, price, usd_amount, side).
    Returns VPIN ∈ [0, 1] или None.
    """
    if len(trades_df) < 2 * VPIN_BUCKETS:
        return None

    # Convert to format ожидаемый calculate_vpin
    trades_list = [
        {
            "size": float(row["usd_amount"]),
            "side": "buy" if str(row["side"]).upper() in ("YES", "BUY") else "sell",
        }
        for _, row in trades_df.iterrows()
    ]
    return BayesianScoreCalculator.calculate_vpin(trades_list, n_buckets=VPIN_BUCKETS)


def market_price_at_resolve(trades_df: pd.DataFrame, resolve_ts: int) -> float | None:
    """Market price на момент resolve (последняя сделка перед/в моменте resolve)."""
    df = trades_df[trades_df["timestamp"] <= resolve_ts]
    if df.empty:
        return None
    return float(df.iloc[-1]["price"])


# ────────────────────────────────────────────────────────────────────────────
# Correlation analysis
# ────────────────────────────────────────────────────────────────────────────

def correlation_breakdown(df: pd.DataFrame) -> dict:
    """Pearson + Spearman corr между VPIN и accuracy_proxy."""
    df_clean = df.dropna(subset=["vpin", "accuracy_proxy"])
    if len(df_clean) < 30:
        return {"n": len(df_clean), "skip_reason": "n<30"}

    pearson = float(df_clean["vpin"].corr(df_clean["accuracy_proxy"]))
    spearman = float(df_clean["vpin"].rank().corr(df_clean["accuracy_proxy"].rank()))

    # Per-category breakdown
    per_cat = {}
    if "category" in df_clean.columns:
        for cat in df_clean["category"].dropna().unique():
            cat_df = df_clean[df_clean["category"] == cat]
            if len(cat_df) < 30:
                continue
            per_cat[cat] = {
                "n": int(len(cat_df)),
                "pearson": round(float(cat_df["vpin"].corr(cat_df["accuracy_proxy"])), 4),
                "spearman": round(float(cat_df["vpin"].rank().corr(cat_df["accuracy_proxy"].rank())), 4),
            }

    return {
        "n": int(len(df_clean)),
        "pearson": round(pearson, 4),
        "spearman": round(spearman, 4),
        "abs_pearson": round(abs(pearson), 4),
        "per_category": per_cat,
    }


def render_verdict(corr: dict) -> str:
    """Markdown verdict block."""
    if "skip_reason" in corr:
        return f"⚠️ **SKIPPED** — {corr['skip_reason']}"
    abs_p = corr["abs_pearson"]
    if abs_p >= INTEGRATE_THRESHOLD:
        return f"✅ **INTEGRATE** — |Pearson| = {abs_p} ≥ {INTEGRATE_THRESHOLD}. Добавить VPIN как factor с w_vpin ≈ 0.15."
    elif abs_p < REJECT_THRESHOLD:
        return f"❌ **REJECT** — |Pearson| = {abs_p} < {REJECT_THRESHOLD}. VPIN не несёт инфо. Document negative result."
    else:
        return f"⚠️ **BORDERLINE** — |Pearson| = {abs_p} ∈ [{REJECT_THRESHOLD}, {INTEGRATE_THRESHOLD}]. Проверить per-category."


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--quant",
        default="/mnt/data/hf_data/quant.parquet",
        help="Path к quant.parquet",
    )
    parser.add_argument(
        "--markets",
        default="/mnt/data/polymarket_dataset/markets.parquet",
        help="Path к markets.parquet (для outcomes)",
    )
    parser.add_argument(
        "--out",
        default="docs/research/2026-04-30-vpin-correlation.md",
        help="Output markdown report",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=5000,
        help="Max resolved markets для анализа (sample)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.quant):
        print(f"ERROR: quant not found: {args.quant}")
        sys.exit(1)
    if not os.path.exists(args.markets):
        print(f"ERROR: markets not found: {args.markets}")
        sys.exit(1)

    con = duckdb.connect()
    con.execute("SET memory_limit='2GB';")
    con.execute("SET threads=2;")

    # Step 1: select resolved markets с достаточным volume
    print(f"Selecting resolved markets...")
    markets_df = con.execute(f"""
        SELECT
            condition_id, category, end_date, winner_outcome, total_volume_usd
        FROM read_parquet('{args.markets}')
        WHERE winner_outcome IN ('YES', 'NO')
          AND end_date IS NOT NULL
          AND total_volume_usd >= {MIN_VOLUME_USD}
        ORDER BY total_volume_usd DESC
        LIMIT {args.max_markets}
    """).fetchdf()
    print(f"  Selected: {len(markets_df):,} markets")

    if len(markets_df) == 0:
        print("ERROR: no resolved markets")
        sys.exit(1)

    # Step 2: per-market VPIN computation
    print(f"\nComputing VPIN per market (window={VPIN_WINDOW_SEC}s)...")
    results = []
    for idx, row in markets_df.iterrows():
        cid = row["condition_id"]
        end_ts = int(pd.Timestamp(row["end_date"]).timestamp())
        start_ts = end_ts - VPIN_WINDOW_SEC

        # Fetch trades for this market in window
        trades_df = con.execute(f"""
            SELECT timestamp, price, usd_amount, side
            FROM read_parquet('{args.quant}')
            WHERE condition_id = '{cid}'
              AND timestamp BETWEEN {start_ts} AND {end_ts}
            ORDER BY timestamp
        """).fetchdf()

        if len(trades_df) < MIN_TRADES_PER_MARKET:
            continue

        vpin = compute_market_vpin(trades_df)
        if vpin is None:
            continue

        mp_at_resolve = market_price_at_resolve(trades_df, end_ts)
        if mp_at_resolve is None:
            continue

        # accuracy_proxy: насколько market_price был "близок к истине"
        # outcome=1 (YES win) → accuracy_proxy = mp (high mp = accurate)
        # outcome=0 (NO win) → accuracy_proxy = 1 - mp
        outcome = 1 if str(row["winner_outcome"]).upper() == "YES" else 0
        accuracy_proxy = mp_at_resolve if outcome == 1 else (1 - mp_at_resolve)

        results.append({
            "condition_id": cid,
            "category": row.get("category") or "Other",
            "vpin": vpin,
            "market_price_resolve": mp_at_resolve,
            "outcome": outcome,
            "accuracy_proxy": accuracy_proxy,
            "n_trades_24h": len(trades_df),
            "volume_usd": float(row["total_volume_usd"]),
        })

        if (len(results)) % 100 == 0:
            print(f"  Processed {len(results)} markets")

    df = pd.DataFrame(results)
    print(f"\nValid markets with VPIN: {len(df):,}")

    if len(df) < 30:
        print("ERROR: too few valid markets for correlation")
        sys.exit(1)

    # Step 3: correlation
    print("\nComputing correlations...")
    corr = correlation_breakdown(df)

    # Render report
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append(f"# VPIN Correlation Study — {pd.Timestamp.now().date()}")
    lines.append("")
    lines.append(f"**Источник:** `{args.quant}` + `{args.markets}`")
    lines.append(f"**Window:** {VPIN_WINDOW_SEC}s ({VPIN_WINDOW_SEC // 3600}h до resolve)")
    lines.append(f"**Min trades:** {MIN_TRADES_PER_MARKET}, Min volume: ${MIN_VOLUME_USD:,.0f}")
    lines.append(f"**Sample size:** {len(df):,} resolved markets")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(render_verdict(corr))
    lines.append("")
    lines.append("## Overall Correlation")
    lines.append("")
    lines.append(f"- **Pearson:** {corr.get('pearson', 'N/A')}")
    lines.append(f"- **Spearman:** {corr.get('spearman', 'N/A')}")
    lines.append(f"- **n:** {corr.get('n', 'N/A')}")
    lines.append("")
    if corr.get("per_category"):
        lines.append("## Per-Category Breakdown")
        lines.append("")
        lines.append("| Category | n | Pearson | Spearman |")
        lines.append("|---|---|---|---|")
        for cat, m in sorted(corr["per_category"].items()):
            lines.append(f"| {cat} | {m['n']:,} | {m['pearson']} | {m['spearman']} |")
        lines.append("")
    lines.append("## Distribution Stats")
    lines.append("")
    lines.append(f"- VPIN min/max: {df['vpin'].min():.3f} / {df['vpin'].max():.3f}")
    lines.append(f"- VPIN mean/median: {df['vpin'].mean():.3f} / {df['vpin'].median():.3f}")
    lines.append(f"- Accuracy proxy mean: {df['accuracy_proxy'].mean():.3f}")
    lines.append(f"- High VPIN (>p95) mean accuracy: {df[df['vpin'] > df['vpin'].quantile(0.95)]['accuracy_proxy'].mean():.3f}")
    lines.append(f"- Low VPIN (<p5) mean accuracy: {df[df['vpin'] < df['vpin'].quantile(0.05)]['accuracy_proxy'].mean():.3f}")
    out_path.write_text("\n".join(lines))

    print(f"\nReport: {out_path}")
    print(f"Verdict: {render_verdict(corr)}")


if __name__ == "__main__":
    main()
