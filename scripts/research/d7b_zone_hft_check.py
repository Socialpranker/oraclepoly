#!/usr/bin/env python3
"""HFT presence check для D7b zone [0.95, 0.97].

Идея: HFT концентрируется в ликвидных markets (Polymarket Liquidity Rewards
платят квадратично за tight spread). Если ROI [0.95, 0.97] держится только
на low-volume markets — нам туда не дойти: high-vol = HFT уже там.

Buckets:
    - thin:   $5K  ≤ lifetime_vol < $30K  (retail-driven, мало HFT)
    - mid:    $30K ≤ lifetime_vol < $100K (mixed)
    - thick:  $100K ≤ lifetime_vol        (HFT-dominated)

Если ROI thin >> mid >> thick → confirm "HFT уже в [0.95, 0.97]" → KILL extension
Если ROI равномерен → edge structural → можем туда идти
"""

from __future__ import annotations

import sys
import time

import duckdb

VOL_BUCKETS = [
    (5_000, 30_000, "thin   ($5K-$30K)"),
    (30_000, 100_000, "mid    ($30K-$100K)"),
    (100_000, 10_000_000_000, "thick  ($100K+)"),
]
ZONES = [
    (0.85, 0.90, "NO_0.85-0.90"),
    (0.90, 0.95, "NO_0.90-0.95"),
    (0.95, 0.97, "NO_0.95-0.97"),
]
ANCHORS = [4, 24, 48]
EXCLUDED_CATS = ["Culture"]

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"


def fetch(con, anchor: int, lo_p: float, hi_p: float, lo_v: float, hi_v: float):
    excl = "', '".join(EXCLUDED_CATS)
    return con.execute(f"""
        SELECT
            m.condition_id,
            m.lifetime_volume_usd,
            m.yes_won,
            s.yes_price
        FROM read_parquet('{MARKETS}') m
        JOIN read_parquet('{SNAPS}') s ON s.condition_id = m.condition_id
        WHERE s.hours_to_resolve = {anchor}
          AND s.yes_price BETWEEN {lo_p} AND {hi_p}
          AND m.yes_won IS NOT NULL
          AND m.lifetime_volume_usd >= {lo_v}
          AND m.lifetime_volume_usd <  {hi_v}
          AND m.category NOT IN ('{excl}')
    """).df()


def calc_roi(df, fee_pct: float = 5.0):
    if len(df) == 0:
        return {"n": 0}
    df = df.copy()
    df["cost_no"] = 1 - df["yes_price"]
    # fee_pct from 100% — снимаем с raw payout
    df["payout"] = (df["yes_won"] == 0).astype(int)
    df["roi"] = (df["payout"] - df["cost_no"]) / df["cost_no"] - fee_pct / 100.0
    return {
        "n": len(df),
        "wr": float(df["payout"].mean()),
        "roi": float(df["roi"].mean()),
        "median_vol": float(df["lifetime_volume_usd"].median()),
    }


def main():
    t0 = time.time()
    print("=" * 90)
    print("D7b HFT-presence check: ROI by (zone × volume bucket × anchor)")
    print("=" * 90)
    print("Hypothesis: HFT концентрируется в high-volume → ROI там должен быть слабее")
    print()

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    rows = []
    for anchor in ANCHORS:
        print(f"\n=== anchor = {anchor}h ===")
        print(f"{'zone':14} {'vol_bucket':22} {'n':>6} {'WR':>6} {'med_vol':>10} {'raw_ROI':>9}")
        print("-" * 80)
        for lo_p, hi_p, zone_label in ZONES:
            for lo_v, hi_v, vol_label in VOL_BUCKETS:
                df = fetch(con, anchor, lo_p, hi_p, lo_v, hi_v)
                r = calc_roi(df)
                if r["n"] == 0:
                    print(f"{zone_label:14} {vol_label:22} {'-':>6}")
                    continue
                rows.append({**r, "anchor": anchor, "zone": zone_label, "vol_bucket": vol_label})
                print(f"{zone_label:14} {vol_label:22} {r['n']:>6,} {r['wr']:>5.1%} "
                      f"${r['median_vol']:>8,.0f} {r['roi']:>+8.1%}")

    # Aggregate per (zone, vol_bucket) across anchors
    print("\n" + "=" * 90)
    print("AGGREGATE: weighted ROI per (zone, vol_bucket)")
    print("=" * 90)
    print(f"{'zone':14} {'vol_bucket':22} {'n':>8} {'WR':>6} {'wROI':>9}")
    print("-" * 80)

    aggs = {}
    for _, _, zone_label in ZONES:
        for _, _, vol_label in VOL_BUCKETS:
            sub = [r for r in rows if r["zone"] == zone_label and r["vol_bucket"] == vol_label]
            n = sum(r["n"] for r in sub)
            if n == 0:
                continue
            wr = sum(r["n"] * r["wr"] for r in sub) / n
            wroi = sum(r["n"] * r["roi"] for r in sub) / n
            aggs[(zone_label, vol_label)] = {"n": n, "wr": wr, "roi": wroi}
            print(f"{zone_label:14} {vol_label:22} {n:>8,} {wr:>5.1%} {wroi:>+8.1%}")

    # HFT presence verdict
    print("\n" + "=" * 90)
    print("VERDICT: HFT presence in extreme zone")
    print("=" * 90)
    for _, _, zone_label in ZONES:
        thin_v = aggs.get((zone_label, "thin   ($5K-$30K)"))
        mid_v = aggs.get((zone_label, "mid    ($30K-$100K)"))
        thick_v = aggs.get((zone_label, "thick  ($100K+)"))
        if not (thin_v and thick_v):
            continue
        diff_thin_thick = thin_v["roi"] - thick_v["roi"]
        print(f"\n{zone_label}:")
        print(f"  thin  ROI = {thin_v['roi']:+.1%} (n={thin_v['n']:,})")
        if mid_v:
            print(f"  mid   ROI = {mid_v['roi']:+.1%} (n={mid_v['n']:,})")
        print(f"  thick ROI = {thick_v['roi']:+.1%} (n={thick_v['n']:,})")
        print(f"  thin - thick = {diff_thin_thick:+.1%}")
        if diff_thin_thick > 0.20:
            print(f"  >>> HFT LIKELY IN THICK — edge concentrated in thin")
        elif diff_thin_thick < -0.05:
            print(f"  >>> NO HFT EFFECT — edge structural, can scale")
        else:
            print(f"  >>> mixed signal — может быть HFT mild")

    print(f"\nTotal: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
