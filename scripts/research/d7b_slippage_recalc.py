#!/usr/bin/env python3
"""D7b zone split-test с post-V2 slippage формулой.

Источник гипотезы: deep research entry_filters_research_2026-05-02.
H1: zone [0.90, 0.95] хуже [0.85, 0.90] после реалистичного slippage.

Базовый расчёт `d7b_refined.py` использует фиксированный fee=5%/7%, не зависящий
от размера ордера и book depth. Этот скрипт пересчитывает per-bucket ROI с
формулой `realistic_slippage_pp = max(2pp, 100*stake/depth_l5, sqrt_law) +
neg_risk_premium`.

Запуск (на VPS):
    cd ~/oracle && /root/oracle/.venv-research/bin/python \\
        scripts/research/d7b_slippage_recalc.py
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import duckdb

# ── Constants (mirror d7b_refined.py) ────────────────────────────────────────

VOL_FLOOR_USD = 5_000
ANCHORS_HOURS = [4, 8, 12, 24, 48, 72]
EXCLUDED_CATS = ["Culture"]
SHADOW_BANKROLL = 1000.0
MAX_STAKE_PCT = 0.005  # 0.5% of bankroll = $5 на $1000 bankroll
DEFAULT_STAKE = SHADOW_BANKROLL * MAX_STAKE_PCT  # $5

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"

# Slippage params
POST_V2_BASELINE_PP = 2.0
SQRT_LAW_Y = 1.0
NEG_RISK_PREMIUM_PP = 1.0


def realistic_slippage_pp(
    stake_usd: float,
    book_depth_l5_usd: float | None,
    sigma_intraday: float | None,
    volume_24h_usd: float | None,
    is_neg_risk: bool = False,
) -> float:
    """Same formula as app/core/slippage.realistic_slippage_pp."""
    if stake_usd <= 0:
        return POST_V2_BASELINE_PP

    effective_depth = book_depth_l5_usd
    if effective_depth is None or effective_depth <= 0:
        if volume_24h_usd and volume_24h_usd > 0:
            effective_depth = volume_24h_usd * 0.05
        else:
            effective_depth = stake_usd

    linear_walk_pp = 100.0 * stake_usd / max(effective_depth, 1.0)

    sqrt_law_pp = 0.0
    if sigma_intraday is not None and volume_24h_usd and volume_24h_usd > 0:
        sqrt_law_pp = (
            SQRT_LAW_Y * sigma_intraday * math.sqrt(stake_usd / volume_24h_usd) * 100.0
        )

    base = max(POST_V2_BASELINE_PP, linear_walk_pp, sqrt_law_pp)
    if is_neg_risk:
        base += NEG_RISK_PREMIUM_PP
    return base


def fetch_signals_per_bucket(con, anchor: int, lo: float, hi: float):
    """Fetch raw signals для (anchor, zone). depth_l5 из canonical если есть."""
    excl_str = "', '".join(EXCLUDED_CATS)
    # canonical V2 имеет lifetime_volume_usd, но НЕ имеет book_depth_l5 в parquet.
    # Используем volume_24h_usd прокси — берём lifetime_volume / market_age_days как
    # rough proxy для daily volume.
    df = con.execute(f"""
        SELECT
            m.condition_id,
            m.category,
            m.lifetime_volume_usd,
            m.yes_won,
            s.yes_price
        FROM read_parquet('{MARKETS}') m
        JOIN read_parquet('{SNAPS}') s
          ON s.condition_id = m.condition_id
        WHERE s.hours_to_resolve = {anchor}
          AND s.yes_price BETWEEN {lo} AND {hi}
          AND m.yes_won IS NOT NULL
          AND m.lifetime_volume_usd >= {VOL_FLOOR_USD}
          AND m.category NOT IN ('{excl_str}')
    """).df()
    return df


def recalc_bucket(df, stake: float = DEFAULT_STAKE,
                  depth_proxy_factor: float = 0.05) -> dict:
    """Применить slippage formula. Depth proxy = lifetime_volume × factor.

    factor=0.05 — оптимистичная оценка ($50K lifetime → $2500 depth_l5)
    factor=0.01 — средняя
    factor=0.001 — pessimistic

    SLIPPAGE MODEL (правильная для extreme price zones):
        cost_no = 1 - yes_price (e.g. 0.08 для yes=0.92)
        depth_no_side ≈ depth_l5 × 0.5 (симметрично-же)
        utilization = stake / depth_no_side
        IMPACT_K = 0.05 (5% удорожание при полном съедании top-5 levels)
        slip_frac_of_no_cost = baseline_frac + IMPACT_K × utilization
        baseline_frac = 0.02 (2% минимум — bid-ask half-spread)
        cost_no_real = cost_no × (1 + slip_frac_of_no_cost)
    """
    if len(df) == 0:
        return {"n": 0}

    df = df.copy()
    df["depth_l5_proxy"] = df["lifetime_volume_usd"] * depth_proxy_factor

    BASELINE_FRAC = 0.02   # 2% half-spread минимум
    IMPACT_K = 0.05        # 5% удорожание при полном съедании top-5 levels

    slip_pp_list = []
    cost_no_real_list = []
    for _, row in df.iterrows():
        cost_no = 1.0 - row["yes_price"]
        depth_no_side = max(row["depth_l5_proxy"] * 0.5, 1.0)
        utilization = min(stake / depth_no_side, 1.0)  # cap 100%
        slip_frac_of_no = BASELINE_FRAC + IMPACT_K * utilization
        cost_no_real = cost_no * (1 + slip_frac_of_no)
        cost_no_real = min(cost_no_real, 0.99)
        # slip_pp в долях полной цены (1.0) — для read-out в таблице
        slip_pp = (cost_no_real - cost_no) * 100
        slip_pp_list.append(slip_pp)
        cost_no_real_list.append(cost_no_real)
    df["slippage_pp"] = slip_pp_list
    df["cost_no_real"] = cost_no_real_list

    # ROI для NO position: если NO win (yes_won=0), payout = 1; иначе 0.
    df["payout"] = (df["yes_won"] == 0).astype(int)
    df["roi"] = (df["payout"] - df["cost_no_real"]) / df["cost_no_real"]

    n = len(df)
    wr = float(df["payout"].mean())
    raw_avg_roi = float(df["roi"].mean())
    std_roi = float(df["roi"].std() or 0)
    dsr = (raw_avg_roi / std_roi) * (max(n, 50) ** 0.5) / 2.0 if std_roi > 0 else 0
    avg_slippage_pp = float(df["slippage_pp"].mean())
    median_slippage_pp = float(df["slippage_pp"].median())

    return {
        "n": n,
        "wr": wr,
        "avg_roi_after_slippage": raw_avg_roi,
        "dsr": dsr,
        "avg_slippage_pp": avg_slippage_pp,
        "median_slippage_pp": median_slippage_pp,
    }


DEPTH_FACTORS = [
    (0.001, "thin (worst case)"),
    (0.01, "medium"),
    (0.05, "deep (best case)"),
]


def main():
    t0 = time.time()
    print("=" * 80)
    print(f"D7b SLIPPAGE RECALC — post-V2 formula, stake=${DEFAULT_STAKE}")
    print("=" * 80)
    print(f"Filter: NO-only, exclude {EXCLUDED_CATS}, vol>=${VOL_FLOOR_USD:,}")
    print(f"Slippage model: cost_no_real = cost_no × (1 + 0.02 + 0.05 × util)")
    print(f"Depth proxy sensitivity: {DEPTH_FACTORS}")
    print()

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    # Pre-fetch raw signals (один раз)
    BUCKETS = [
        (0.80, 0.85, "NO_0.80-0.85"),  # extension low
        (0.85, 0.90, "NO_0.85-0.90"),
        (0.90, 0.95, "NO_0.90-0.95"),
        (0.95, 0.97, "NO_0.95-0.97"),  # extension high
    ]
    bucket_data = {}
    for anchor in ANCHORS_HOURS:
        for lo, hi, label in BUCKETS:
            bucket_data[(anchor, label)] = fetch_signals_per_bucket(con, anchor, lo, hi)

    rows = []
    for factor, factor_label in DEPTH_FACTORS:
        print(f"\n=== DEPTH FACTOR = {factor} ({factor_label}) ===")
        print(f"{'anchor':>7} {'bucket':14} {'n':>6} {'WR':>6} "
              f"{'avg_slip_pp':>11} {'med_slip_pp':>11} {'roi_AFTER':>10} {'DSR':>6}")
        print("-" * 80)
        for anchor in ANCHORS_HOURS:
            for lo, hi, label in BUCKETS:
                df = bucket_data[(anchor, label)]
                r = recalc_bucket(df, stake=DEFAULT_STAKE, depth_proxy_factor=factor)
                r["anchor_h"] = anchor
                r["bucket"] = label
                r["depth_factor"] = factor
                rows.append(r)
                if r["n"] == 0:
                    continue
                print(f"{anchor:>5}h {label:14} {r['n']:>6,} {r['wr']:>5.1%} "
                      f"{r['avg_slippage_pp']:>10.2f} {r['median_slippage_pp']:>10.2f} "
                      f"{r['avg_roi_after_slippage']:>+9.1%} {r['dsr']:>+5.2f}")

    # Aggregate per (factor, bucket)
    print()
    print("=" * 80)
    print("AGGREGATE: weighted ROI per (depth_factor, bucket)")
    print("=" * 80)
    print(f"{'depth_factor':>14} {'bucket':14} {'n':>8} {'wROI_after':>11} {'avg_slip':>10}")
    print("-" * 80)

    BUCKET_LABELS = ["NO_0.80-0.85", "NO_0.85-0.90", "NO_0.90-0.95", "NO_0.95-0.97"]
    aggs = {}
    for factor, _ in DEPTH_FACTORS:
        for bucket in BUCKET_LABELS:
            sub = [r for r in rows if r["depth_factor"] == factor and r["bucket"] == bucket and r["n"] > 0]
            n_total = sum(r["n"] for r in sub)
            wroi = sum(r["n"] * r["avg_roi_after_slippage"] for r in sub) / max(n_total, 1)
            avg_slip = sum(r["n"] * r["avg_slippage_pp"] for r in sub) / max(n_total, 1)
            aggs[(factor, bucket)] = {"n": n_total, "wroi": wroi, "avg_slip": avg_slip}
            print(f"{factor:>14.4f} {bucket:14} {n_total:>8,} {wroi:>+10.1%} {avg_slip:>9.2f}pp")

    # Per-factor zone ranking
    print()
    print("=" * 80)
    print("ZONE RANKING per depth_factor (best → worst by ROI)")
    print("=" * 80)
    for factor, factor_label in DEPTH_FACTORS:
        print(f"\nfactor={factor:.4f} ({factor_label}):")
        zone_results = [(b, aggs[(factor, b)]) for b in BUCKET_LABELS if aggs[(factor, b)]["n"] > 0]
        zone_results.sort(key=lambda x: -x[1]["wroi"])
        for rank, (bucket, agg) in enumerate(zone_results, start=1):
            marker = "🥇" if rank == 1 else ("🥈" if rank == 2 else "  ")
            print(f"  {marker} {bucket:14} ROI={agg['wroi']:>+7.1%} n={agg['n']:>6,} slip={agg['avg_slip']:.2f}pp")

    print()
    print(f"Total: {time.time() - t0:.1f}s")

    # Save report
    out_dir = Path("/root/oracle/products/d7-pre-resolution-convergence-fade")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "d7b_slippage_recalc.md"

    lines = [
        "# D7b Slippage Recalc — post-V2 formula (sensitivity sweep)",
        "",
        f"**Stake:** ${DEFAULT_STAKE}",
        f"**Slippage model:** cost_no_real = cost_no × (1 + 0.02 + 0.05 × utilization)",
        f"**utilization** = stake / (depth_l5 × 0.5)",
        f"**depth_l5 proxy** = lifetime_volume × factor (sensitivity: 0.001 / 0.01 / 0.05)",
        "",
        "## Per-bucket per-factor",
        "",
        "| factor | anchor | bucket | n | WR | avg_slip_pp | roi_after | DSR |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r["n"] == 0:
            continue
        lines.append(
            f"| {r['depth_factor']:.4f} | {r['anchor_h']}h | {r['bucket']} | "
            f"{r['n']:,} | {r['wr']:.1%} | {r['avg_slippage_pp']:.2f} | "
            f"{r['avg_roi_after_slippage']:+.1%} | {r['dsr']:+.2f} |"
        )

    lines.extend([
        "",
        "## Aggregate per (factor, bucket)",
        "",
        "| factor | bucket | n | weighted_ROI_after | avg_slip_pp |",
        "|---|---|---|---|---|",
    ])
    for (factor, bucket), v in aggs.items():
        lines.append(
            f"| {factor:.4f} | {bucket} | {v['n']:,} | {v['wroi']:+.1%} | {v['avg_slip']:.2f} |"
        )

    out_path.write_text("\n".join(lines))
    print(f"\nReport saved: {out_path}")


if __name__ == "__main__":
    sys.exit(main() or 0)
