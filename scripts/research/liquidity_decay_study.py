#!/usr/bin/env python3
"""Liquidity decay correlation study — Phase 5.2 (independent от pit_v3).

Гипотеза: markets где `decay_pct > 80%` (>80% volume traded в last 24h vs total
market lifetime) имеют thin liquidity → orders ходят без impact → contrarian
entries бесполезны. Если decay_pct корреляции с outcome accuracy:
  - high decay + correct outcome → "trapped" поздние traders
  - low decay + correct outcome → market_price stable, accurate

Логика:
1. Resolved markets с volume ≥ $10K и ≥100 trades.
2. Per-market:
   - `total_lifetime_volume_usd` = sum(usd_amount) all trades
   - `last_24h_volume_usd` = sum(usd_amount) in [resolve_ts - 24h, resolve_ts]
   - `decay_pct` = last_24h / total
   - `mp_at_resolve` = last price ≤ resolve_ts
   - `accuracy_proxy` = mp_at_resolve if outcome=YES else (1 - mp)
3. Correlation analysis:
   - decay_pct vs accuracy_proxy (overall + per-category)
   - Bin analysis: low/mid/high decay tier accuracy

Verdict для Phase 5.2:
- |corr| ≥ 0.10 → INTEGRATE как gate (skip if decay > 80%)
- |corr| < 0.05 → REJECT
- 0.05-0.10 → BORDERLINE per-category

Usage:
    python3 scripts/research/liquidity_decay_study.py \\
      --quant /mnt/data/hf_data/quant.parquet \\
      --markets /mnt/data/polymarket_dataset/markets.parquet \\
      --out docs/research/2026-04-30-liquidity-decay.md \\
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

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

MIN_TRADES_PER_MARKET = 100
MIN_VOLUME_USD = 10_000.0
DECAY_WINDOW_SEC = 24 * 3600  # 24h до resolve

# Acceptance criteria
INTEGRATE_THRESHOLD = 0.10
REJECT_THRESHOLD = 0.05

# Bins для tier analysis
DECAY_BINS = [
    ("low",   0.0, 0.30),   # <30% decay — стабильный market
    ("mid",   0.30, 0.60),  # 30-60% — нормально
    ("high",  0.60, 0.80),  # 60-80% — поздняя активность
    ("extreme", 0.80, 1.01),  # >80% — pre-resolution rush
]


# ────────────────────────────────────────────────────────────────────────────
# Per-market metrics
# ────────────────────────────────────────────────────────────────────────────

def compute_decay_metrics(trades_df: pd.DataFrame, resolve_ts: int) -> dict | None:
    """Per-market liquidity decay metrics.

    Returns dict с decay_pct, total_volume, last_24h_volume, mp_at_resolve.
    None если недостаточно trades.
    """
    if len(trades_df) < MIN_TRADES_PER_MARKET:
        return None

    total_volume = float(trades_df["usd_amount"].sum())
    if total_volume <= 0:
        return None

    window_start = resolve_ts - DECAY_WINDOW_SEC
    last_24h = trades_df[trades_df["timestamp"] >= window_start]
    last_24h_volume = float(last_24h["usd_amount"].sum())

    decay_pct = last_24h_volume / total_volume if total_volume > 0 else 0.0

    # mp_at_resolve = last trade ≤ resolve_ts
    pre_resolve = trades_df[trades_df["timestamp"] <= resolve_ts]
    if pre_resolve.empty:
        return None
    mp = float(pre_resolve.iloc[-1]["price"])

    return {
        "total_volume": total_volume,
        "last_24h_volume": last_24h_volume,
        "decay_pct": decay_pct,
        "mp_at_resolve": mp,
        "n_trades": len(trades_df),
    }


def assign_decay_tier(decay_pct: float) -> str | None:
    """Map decay_pct → tier label."""
    for label, lo, hi in DECAY_BINS:
        if lo <= decay_pct < hi:
            return label
    return None


# ────────────────────────────────────────────────────────────────────────────
# Correlation + tier analysis
# ────────────────────────────────────────────────────────────────────────────

def correlation_breakdown(df: pd.DataFrame) -> dict:
    """Pearson + Spearman + per-category."""
    df_clean = df.dropna(subset=["decay_pct", "accuracy_proxy"])
    if len(df_clean) < 30:
        return {"n": len(df_clean), "skip_reason": "n<30"}

    pearson = float(df_clean["decay_pct"].corr(df_clean["accuracy_proxy"]))
    spearman = float(df_clean["decay_pct"].rank().corr(df_clean["accuracy_proxy"].rank()))

    per_cat = {}
    if "category" in df_clean.columns:
        for cat in df_clean["category"].dropna().unique():
            cat_df = df_clean[df_clean["category"] == cat]
            if len(cat_df) < 30:
                continue
            per_cat[cat] = {
                "n": int(len(cat_df)),
                "pearson": round(float(cat_df["decay_pct"].corr(cat_df["accuracy_proxy"])), 4),
                "spearman": round(float(cat_df["decay_pct"].rank().corr(cat_df["accuracy_proxy"].rank())), 4),
            }

    return {
        "n": int(len(df_clean)),
        "pearson": round(pearson, 4),
        "spearman": round(spearman, 4),
        "abs_pearson": round(abs(pearson), 4),
        "per_category": per_cat,
    }


def tier_breakdown(df: pd.DataFrame) -> list[dict]:
    """Accuracy proxy mean per decay_tier."""
    df = df.copy()
    df["decay_tier"] = df["decay_pct"].apply(assign_decay_tier)
    df = df[df["decay_tier"].notna()]

    rows = []
    for tier_name, _, _ in DECAY_BINS:
        sub = df[df["decay_tier"] == tier_name]
        if len(sub) < 10:
            rows.append({"tier": tier_name, "n": len(sub), "skip": True})
            continue
        rows.append({
            "tier": tier_name,
            "n": int(len(sub)),
            "accuracy_mean": round(float(sub["accuracy_proxy"].mean()), 4),
            "accuracy_std": round(float(sub["accuracy_proxy"].std()), 4),
            "outcome_yes_pct": round(float((sub["outcome"] == 1).mean()), 4),
        })
    return rows


def render_verdict(corr: dict, tiers: list[dict]) -> str:
    """Markdown verdict."""
    if "skip_reason" in corr:
        return f"⚠️ **SKIPPED** — {corr['skip_reason']}"
    abs_p = corr["abs_pearson"]

    extreme_tier = next((t for t in tiers if t.get("tier") == "extreme" and not t.get("skip")), None)
    low_tier = next((t for t in tiers if t.get("tier") == "low" and not t.get("skip")), None)

    tier_diff = None
    if extreme_tier and low_tier:
        tier_diff = abs(extreme_tier["accuracy_mean"] - low_tier["accuracy_mean"])

    if abs_p >= INTEGRATE_THRESHOLD:
        return (f"✅ **INTEGRATE** — |Pearson| = {abs_p} ≥ {INTEGRATE_THRESHOLD}. "
                f"Add decay gate: skip entries when decay_pct > 0.80.")
    elif abs_p < REJECT_THRESHOLD and (tier_diff is None or tier_diff < 0.05):
        return f"❌ **REJECT** — |Pearson| = {abs_p} < {REJECT_THRESHOLD}. Decay не несёт информации."
    else:
        return (f"⚠️ **BORDERLINE** — |Pearson| = {abs_p} ∈ [{REJECT_THRESHOLD}, {INTEGRATE_THRESHOLD}]. "
                f"Tier diff (extreme vs low): {tier_diff}. Проверить per-category.")


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--quant",
        default="/mnt/data/hf_data/quant.parquet",
    )
    parser.add_argument(
        "--markets",
        default="/mnt/data/polymarket_dataset/markets.parquet",
    )
    parser.add_argument(
        "--out",
        default="docs/research/2026-04-30-liquidity-decay.md",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=5000,
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

    print("Selecting resolved markets...")
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

    print(f"\nComputing decay metrics per market (window={DECAY_WINDOW_SEC}s)...")
    results = []
    for idx, row in markets_df.iterrows():
        cid = row["condition_id"]
        end_ts = int(pd.Timestamp(row["end_date"]).timestamp())

        # Per-market: ВСЕ trades (для total volume) — это full lifetime
        trades_df = con.execute(f"""
            SELECT timestamp, price, usd_amount
            FROM read_parquet('{args.quant}')
            WHERE condition_id = '{cid}'
            ORDER BY timestamp
        """).fetchdf()

        metrics = compute_decay_metrics(trades_df, end_ts)
        if metrics is None:
            continue

        outcome = 1 if str(row["winner_outcome"]).upper() == "YES" else 0
        accuracy_proxy = metrics["mp_at_resolve"] if outcome == 1 else (1 - metrics["mp_at_resolve"])

        results.append({
            "condition_id": cid,
            "category": row.get("category") or "Other",
            "decay_pct": metrics["decay_pct"],
            "total_volume_usd": metrics["total_volume"],
            "last_24h_volume_usd": metrics["last_24h_volume"],
            "n_trades": metrics["n_trades"],
            "mp_at_resolve": metrics["mp_at_resolve"],
            "outcome": outcome,
            "accuracy_proxy": accuracy_proxy,
        })

        if (len(results)) % 100 == 0:
            print(f"  Processed {len(results)} markets")

    df = pd.DataFrame(results)
    print(f"\nValid markets: {len(df):,}")

    if len(df) < 30:
        print("ERROR: too few valid markets")
        sys.exit(1)

    print("\nComputing correlations + tier breakdown...")
    corr = correlation_breakdown(df)
    tiers = tier_breakdown(df)

    # Render report
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append(f"# Liquidity Decay Study — {pd.Timestamp.now().date()}")
    lines.append("")
    lines.append(f"**Источник:** `{args.quant}` + `{args.markets}`")
    lines.append(f"**Window:** {DECAY_WINDOW_SEC}s ({DECAY_WINDOW_SEC // 3600}h до resolve)")
    lines.append(f"**Sample:** {len(df):,} resolved markets")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(render_verdict(corr, tiers))
    lines.append("")
    lines.append("## Overall Correlation (decay_pct vs accuracy_proxy)")
    lines.append("")
    lines.append(f"- **Pearson:** {corr.get('pearson', 'N/A')}")
    lines.append(f"- **Spearman:** {corr.get('spearman', 'N/A')}")
    lines.append(f"- **n:** {corr.get('n', 'N/A')}")
    lines.append("")
    lines.append("## Tier Breakdown")
    lines.append("")
    lines.append("| Tier | Range | n | accuracy mean | accuracy std | outcome=YES % |")
    lines.append("|---|---|---|---|---|---|")
    for tier in tiers:
        if tier.get("skip"):
            lines.append(f"| {tier['tier']} | - | {tier['n']} | SKIP (n<10) | - | - |")
            continue
        bin_def = next(b for b in DECAY_BINS if b[0] == tier["tier"])
        lines.append(
            f"| {tier['tier']} | [{bin_def[1]:.2f}, {bin_def[2]:.2f}) | {tier['n']:,} | "
            f"{tier['accuracy_mean']} | {tier['accuracy_std']} | {tier['outcome_yes_pct']} |"
        )
    lines.append("")
    if corr.get("per_category"):
        lines.append("## Per-Category Correlation")
        lines.append("")
        lines.append("| Category | n | Pearson | Spearman |")
        lines.append("|---|---|---|---|")
        for cat, m in sorted(corr["per_category"].items()):
            lines.append(f"| {cat} | {m['n']:,} | {m['pearson']} | {m['spearman']} |")
        lines.append("")
    lines.append("## Distribution")
    lines.append("")
    lines.append(f"- decay_pct min/p25/p50/p75/p95/max: "
                 f"{df['decay_pct'].min():.3f} / "
                 f"{df['decay_pct'].quantile(0.25):.3f} / "
                 f"{df['decay_pct'].median():.3f} / "
                 f"{df['decay_pct'].quantile(0.75):.3f} / "
                 f"{df['decay_pct'].quantile(0.95):.3f} / "
                 f"{df['decay_pct'].max():.3f}")

    out_path.write_text("\n".join(lines))

    print(f"\nReport: {out_path}")
    print(f"Verdict: {render_verdict(corr, tiers)}")


if __name__ == "__main__":
    main()
