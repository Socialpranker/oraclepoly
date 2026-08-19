#!/usr/bin/env python3
"""Pre-resolution price drift study — Phase 5.3 (independent от pit_v3).

Гипотеза: за 24-72h до resolve цена дрейфует к истине. Если drift коррелирует
с outcome → можно использовать как **exit signal**: закрывать позиции с
противоположным drift.

Это **exit-time** факор, не entry. Логика применения:
- Если open позиция YES, drift_24h_to_resolve < 0 (price падает) → close
- Если open позиция NO, drift_24h_to_resolve > 0 (price растёт) → close

Логика анализа:
1. Resolved markets с volume ≥ $10K, ≥100 trades.
2. Per-market:
   - mp_24h_before_resolve = price 24h до resolve_ts
   - mp_at_resolve = price at resolve_ts
   - drift_24h = mp_at_resolve - mp_24h_before
3. Correlation: drift vs realized_outcome (как accuracy proxy)
   - High drift + outcome=YES → drift предсказал движение в правильную сторону
   - High drift + outcome=NO → drift был misleading

Verdict для Phase 5.3:
- |corr| ≥ 0.10 → INTEGRATE как exit signal
- |corr| < 0.05 → REJECT
- 0.05-0.10 → BORDERLINE

Usage:
    python3 scripts/research/pre_resolution_drift_study.py \\
      --quant /mnt/data/hf_data/quant.parquet \\
      --markets /mnt/data/polymarket_dataset/markets.parquet \\
      --out docs/research/2026-04-30-pre-resolution-drift.md
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

MIN_TRADES_PER_MARKET = 100
MIN_VOLUME_USD = 10_000.0
DRIFT_WINDOW_SEC = 24 * 3600  # 24h до resolve

INTEGRATE_THRESHOLD = 0.10
REJECT_THRESHOLD = 0.05

# Bins для drift tier
DRIFT_BINS = [
    ("strong_down", -1.0, -0.10),  # drift ≤ -10pp
    ("mid_down",    -0.10, -0.03),
    ("flat",        -0.03,  0.03),  # ±3pp = neutral
    ("mid_up",       0.03,  0.10),
    ("strong_up",    0.10,  1.01),
]


# ────────────────────────────────────────────────────────────────────────────
# Per-market drift
# ────────────────────────────────────────────────────────────────────────────

def compute_drift_metrics(trades_df: pd.DataFrame, resolve_ts: int) -> dict | None:
    """Pre-resolution drift: mp_at_resolve - mp_24h_before."""
    if len(trades_df) < MIN_TRADES_PER_MARKET:
        return None

    pre_resolve = trades_df[trades_df["timestamp"] <= resolve_ts]
    if len(pre_resolve) < MIN_TRADES_PER_MARKET:
        return None

    window_start = resolve_ts - DRIFT_WINDOW_SEC

    # mp_at_resolve — последняя сделка ≤ resolve_ts
    mp_resolve = float(pre_resolve.iloc[-1]["price"])

    # mp_24h_before — последняя сделка ≤ window_start
    before_window = trades_df[trades_df["timestamp"] <= window_start]
    if before_window.empty:
        return None
    mp_before = float(before_window.iloc[-1]["price"])

    drift = mp_resolve - mp_before

    return {
        "mp_24h_before": mp_before,
        "mp_at_resolve": mp_resolve,
        "drift_24h": drift,
        "n_trades": len(trades_df),
    }


def assign_drift_tier(drift: float) -> str | None:
    for label, lo, hi in DRIFT_BINS:
        if lo <= drift < hi:
            return label
    return None


# ────────────────────────────────────────────────────────────────────────────
# Correlation + tier analysis
# ────────────────────────────────────────────────────────────────────────────

def correlation_breakdown(df: pd.DataFrame) -> dict:
    """Pearson + Spearman: drift_24h vs outcome."""
    df_clean = df.dropna(subset=["drift_24h", "outcome"])
    if len(df_clean) < 30:
        return {"n": len(df_clean), "skip_reason": "n<30"}

    pearson = float(df_clean["drift_24h"].corr(df_clean["outcome"]))
    spearman = float(df_clean["drift_24h"].rank().corr(df_clean["outcome"].rank()))

    per_cat = {}
    if "category" in df_clean.columns:
        for cat in df_clean["category"].dropna().unique():
            cat_df = df_clean[df_clean["category"] == cat]
            if len(cat_df) < 30:
                continue
            per_cat[cat] = {
                "n": int(len(cat_df)),
                "pearson": round(float(cat_df["drift_24h"].corr(cat_df["outcome"])), 4),
                "spearman": round(float(cat_df["drift_24h"].rank().corr(cat_df["outcome"].rank())), 4),
            }

    return {
        "n": int(len(df_clean)),
        "pearson": round(pearson, 4),
        "spearman": round(spearman, 4),
        "abs_pearson": round(abs(pearson), 4),
        "per_category": per_cat,
    }


def tier_breakdown(df: pd.DataFrame) -> list[dict]:
    """Outcome=YES rate per drift tier."""
    df = df.copy()
    df["drift_tier"] = df["drift_24h"].apply(assign_drift_tier)
    df = df[df["drift_tier"].notna()]

    rows = []
    for tier_name, _, _ in DRIFT_BINS:
        sub = df[df["drift_tier"] == tier_name]
        if len(sub) < 10:
            rows.append({"tier": tier_name, "n": len(sub), "skip": True})
            continue
        rows.append({
            "tier": tier_name,
            "n": int(len(sub)),
            "outcome_yes_pct": round(float((sub["outcome"] == 1).mean()), 4),
            "drift_mean": round(float(sub["drift_24h"].mean()), 4),
        })
    return rows


def render_verdict(corr: dict, tiers: list[dict]) -> str:
    if "skip_reason" in corr:
        return f"⚠️ **SKIPPED** — {corr['skip_reason']}"
    abs_p = corr["abs_pearson"]

    strong_up = next((t for t in tiers if t.get("tier") == "strong_up" and not t.get("skip")), None)
    strong_down = next((t for t in tiers if t.get("tier") == "strong_down" and not t.get("skip")), None)

    tier_diff = None
    if strong_up and strong_down:
        tier_diff = abs(strong_up["outcome_yes_pct"] - strong_down["outcome_yes_pct"])

    if abs_p >= INTEGRATE_THRESHOLD:
        return (f"✅ **INTEGRATE** — |Pearson| = {abs_p} ≥ {INTEGRATE_THRESHOLD}. "
                f"Add drift exit signal: close YES position if drift < -3pp, NO if drift > +3pp.")
    elif abs_p < REJECT_THRESHOLD and (tier_diff is None or tier_diff < 0.10):
        return f"❌ **REJECT** — |Pearson| = {abs_p} < {REJECT_THRESHOLD}. Drift не предсказывает outcome."
    else:
        return (f"⚠️ **BORDERLINE** — |Pearson| = {abs_p} ∈ [{REJECT_THRESHOLD}, {INTEGRATE_THRESHOLD}]. "
                f"Tier diff (strong_up vs strong_down): {tier_diff}.")


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
        default="docs/research/2026-04-30-pre-resolution-drift.md",
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

    print(f"\nComputing drift metrics (window={DRIFT_WINDOW_SEC}s)...")
    results = []
    for idx, row in markets_df.iterrows():
        cid = row["condition_id"]
        end_ts = int(pd.Timestamp(row["end_date"]).timestamp())

        trades_df = con.execute(f"""
            SELECT timestamp, price
            FROM read_parquet('{args.quant}')
            WHERE condition_id = '{cid}'
            ORDER BY timestamp
        """).fetchdf()

        metrics = compute_drift_metrics(trades_df, end_ts)
        if metrics is None:
            continue

        outcome = 1 if str(row["winner_outcome"]).upper() == "YES" else 0

        results.append({
            "condition_id": cid,
            "category": row.get("category") or "Other",
            "drift_24h": metrics["drift_24h"],
            "mp_24h_before": metrics["mp_24h_before"],
            "mp_at_resolve": metrics["mp_at_resolve"],
            "n_trades": metrics["n_trades"],
            "outcome": outcome,
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
    lines.append(f"# Pre-Resolution Drift Study — {pd.Timestamp.now().date()}")
    lines.append("")
    lines.append(f"**Источник:** `{args.quant}` + `{args.markets}`")
    lines.append(f"**Window:** {DRIFT_WINDOW_SEC}s ({DRIFT_WINDOW_SEC // 3600}h до resolve)")
    lines.append(f"**Sample:** {len(df):,} resolved markets")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(render_verdict(corr, tiers))
    lines.append("")
    lines.append("## Overall Correlation (drift_24h vs outcome)")
    lines.append("")
    lines.append(f"- **Pearson:** {corr.get('pearson', 'N/A')}")
    lines.append(f"- **Spearman:** {corr.get('spearman', 'N/A')}")
    lines.append(f"- **n:** {corr.get('n', 'N/A')}")
    lines.append("")
    lines.append("## Tier Breakdown — outcome=YES rate")
    lines.append("")
    lines.append("| Tier | Range | n | drift_mean | outcome=YES % |")
    lines.append("|---|---|---|---|---|")
    for tier in tiers:
        if tier.get("skip"):
            lines.append(f"| {tier['tier']} | - | {tier['n']} | SKIP (n<10) | - |")
            continue
        bin_def = next(b for b in DRIFT_BINS if b[0] == tier["tier"])
        lines.append(
            f"| {tier['tier']} | [{bin_def[1]:+.2f}, {bin_def[2]:+.2f}) | "
            f"{tier['n']:,} | {tier['drift_mean']:+.4f} | {tier['outcome_yes_pct']} |"
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
    lines.append(f"- drift_24h min/p25/p50/p75/p95/max: "
                 f"{df['drift_24h'].min():+.3f} / "
                 f"{df['drift_24h'].quantile(0.25):+.3f} / "
                 f"{df['drift_24h'].median():+.3f} / "
                 f"{df['drift_24h'].quantile(0.75):+.3f} / "
                 f"{df['drift_24h'].quantile(0.95):+.3f} / "
                 f"{df['drift_24h'].max():+.3f}")

    out_path.write_text("\n".join(lines))

    print(f"\nReport: {out_path}")
    print(f"Verdict: {render_verdict(corr, tiers)}")


if __name__ == "__main__":
    main()
