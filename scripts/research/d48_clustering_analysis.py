"""D48 round-number clustering analysis — Phase 0.

Goal:
  1. Verify Bloomfield 2025 claim of 3.73x clustering at integer prices.
  2. Identify which round numbers cluster strongest on Polymarket.
  3. Compute base rates для downstream bounce/momentum backtest.

Method:
  - Load trade_buckets.parquet (~15.5M hourly buckets)
  - Compute vwap_yes histogram at 0.01 resolution
  - Compare actual frequency at round numbers vs uniform baseline
  - Per category breakdown (read from markets_clean.parquet)

Output: docs/research/D48_clustering_analysis.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

CANONICAL_BUCKETS = "/mnt/data/canonical/trade_buckets.parquet"
CANONICAL_MARKETS = "/mnt/data/canonical/markets_clean.parquet"

ROUND_NUMBERS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.50, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
PRICE_BINS = np.linspace(0.01, 0.99, 99)  # 0.01..0.99 step 0.01


def main(args) -> int:
    print(f"Loading {CANONICAL_BUCKETS}")
    cols = ["condition_id", "vwap_yes", "n_trades", "total_volume_usd"]
    df = pq.read_table(CANONICAL_BUCKETS, columns=cols).to_pandas()
    print(f"  {len(df):,} buckets total")

    # Filter: liquid buckets only
    df = df[(df["n_trades"] >= 3) & (df["total_volume_usd"] >= 50)]
    print(f"  {len(df):,} buckets after liquidity filter")

    print(f"Loading {CANONICAL_MARKETS}")
    markets = pq.read_table(
        CANONICAL_MARKETS,
        columns=["condition_id", "category"]
    ).to_pandas()
    df = df.merge(markets, on="condition_id", how="left")
    df["category"] = df["category"].fillna("Unknown")
    df = df[df["vwap_yes"].between(0.01, 0.99, inclusive="both")]
    print(f"  {len(df):,} buckets with vwap in (0,1)")

    # Round vwap to 2 decimals (0.50, 0.51, etc.)
    df["price_2dp"] = (df["vwap_yes"] * 100).round().astype(int) / 100

    # === Histogram at 0.01 resolution ===
    print("\n=== Overall clustering at 0.01 resolution ===")
    hist = df["price_2dp"].value_counts().sort_index()
    total = len(df)
    expected_per_bin = total / 99  # uniform baseline

    print(f"Total buckets: {total:,}")
    print(f"Expected per bin (uniform): {expected_per_bin:.0f}")
    print()

    cluster_rows = []
    for r in ROUND_NUMBERS:
        n = int(hist.get(r, 0))
        clustering_ratio = n / expected_per_bin if expected_per_bin > 0 else 0
        # Compare with neighbours (avg of r-0.01 and r+0.01)
        n_left = int(hist.get(round(r - 0.01, 2), 0))
        n_right = int(hist.get(round(r + 0.01, 2), 0))
        neighbour_avg = (n_left + n_right) / 2 if (n_left + n_right) > 0 else expected_per_bin
        local_ratio = n / neighbour_avg if neighbour_avg > 0 else 0
        cluster_rows.append({
            "price": r,
            "n": n,
            "vs_uniform": clustering_ratio,
            "vs_neighbours": local_ratio,
        })
        print(f"  price={r:.2f}: n={n:>7,}  ratio vs uniform={clustering_ratio:>5.2f}x  vs neighbours={local_ratio:>5.2f}x")

    # === Per category ===
    print("\n=== Per category clustering at 0.50 ===")
    cat_rows = []
    for cat, sub in df.groupby("category"):
        if len(sub) < 1000:
            continue
        n_at_050 = int((sub["price_2dp"] == 0.50).sum())
        expected = len(sub) / 99
        ratio = n_at_050 / expected
        cat_rows.append({
            "category": cat,
            "n_buckets": len(sub),
            "n_at_050": n_at_050,
            "ratio_vs_uniform": ratio,
        })
        print(f"  {cat:30s}  n={len(sub):>7,}  n_at_0.50={n_at_050:>5,}  ratio={ratio:.2f}x")

    # === Strongest round numbers across all data ===
    print("\n=== Top 10 most-frequent price levels (any) ===")
    top = hist.nlargest(15)
    for price, n in top.items():
        ratio = n / expected_per_bin
        marker = "  ROUND" if price in ROUND_NUMBERS else ""
        print(f"  price={price:.2f}: n={n:>7,}  ratio={ratio:>5.2f}x{marker}")

    # Markdown report
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# D48 Round-Number Clustering Analysis",
        "",
        f"Source: {CANONICAL_BUCKETS} ({total:,} buckets after liquidity filter)",
        "",
        "## Overall clustering at standard round numbers",
        "",
        "| Price | n buckets | vs uniform (99 bins) | vs neighbours avg |",
        "|---|---|---|---|",
    ]
    for r in cluster_rows:
        lines.append(f"| {r['price']:.2f} | {r['n']:,} | {r['vs_uniform']:.2f}x | {r['vs_neighbours']:.2f}x |")

    lines += [
        "",
        "## Per category (clustering at 0.50)",
        "",
        "| Category | n buckets | n at 0.50 | ratio vs uniform |",
        "|---|---|---|---|",
    ]
    for r in cat_rows:
        lines.append(
            f"| {r['category']} | {r['n_buckets']:,} | {r['n_at_050']:,} | {r['ratio_vs_uniform']:.2f}x |"
        )

    lines += [
        "",
        "## Top 15 most-frequent price levels",
        "",
        "| Price | n buckets | ratio vs uniform | round? |",
        "|---|---|---|---|",
    ]
    for price, n in top.items():
        ratio = n / expected_per_bin
        marker = "YES" if price in ROUND_NUMBERS else ""
        lines.append(f"| {price:.2f} | {n:,} | {ratio:.2f}x | {marker} |")

    out.write_text("\n".join(lines) + "\n")
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="/app/docs/research/D48_clustering_analysis.md")
    sys.exit(main(p.parse_args()))
