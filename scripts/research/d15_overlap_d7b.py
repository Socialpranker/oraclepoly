#!/usr/bin/env python3
"""D15 overlap check: incremental alpha над D7b extended Sports subset.

D7b extended (PR #396): zone [0.85, 0.97], all 6 anchors, vol_floor=$5K,
exclude Culture, для extreme zone vol<$100K.

D15 best: zone [0.85, 0.92], all 6 anchors, vol_floor=$5K, Sports only,
exclude neg_risk + fight keywords.

Вопрос: D15 set ⊆ D7b set? Если да — D15 не добавляет alpha.
"""

from __future__ import annotations

import duckdb

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"
ANCHORS = [4, 8, 12, 24, 48, 72]
N_DAYS = 821
LIVE_BANKROLL = 5_000.0
MAX_STAKE = LIVE_BANKROLL * 0.02  # $100
FEE_PCT = 1.5
HAIRCUT = 0.5

FIGHT_KW = ["ufc", "knockout", "boxing", "mma", "championship belt"]


def fetch(con, where: str) -> "duckdb.DuckDBPyConnection":
    anchors_str = ",".join(str(h) for h in ANCHORS)
    return con.execute(f"""
        WITH signals AS (
            SELECT m.condition_id, m.event_id, m.category, m.yes_won,
                   s.yes_price, s.hours_to_resolve, m.lifetime_volume_usd, m.question, m.neg_risk,
                   ROW_NUMBER() OVER (PARTITION BY m.condition_id ORDER BY s.hours_to_resolve) AS rn
            FROM read_parquet('{MARKETS}') m
            JOIN read_parquet('{SNAPS}') s ON s.condition_id = m.condition_id
            WHERE s.hours_to_resolve IN ({anchors_str})
              AND m.yes_won IS NOT NULL
              AND m.lifetime_volume_usd >= 5000
              {where}
        )
        SELECT condition_id, event_id, category, yes_won, yes_price,
               hours_to_resolve, lifetime_volume_usd, question, neg_risk
        FROM signals WHERE rn = 1
    """).df()


def calc_pnl(df, slip_pp=3.0):
    if len(df) == 0:
        return {"n": 0, "daily_real": 0.0, "wr": None, "avg_roi": None}
    df = df.copy()
    df["cost_no"] = (1 - df["yes_price"] + slip_pp / 100.0).clip(0.01, 0.99)
    df["payout"] = (df["yes_won"] == 0).astype(int)
    df["roi"] = (df["payout"] / df["cost_no"]) - 1.0
    fee_drag = MAX_STAKE * FEE_PCT / 100.0
    net_pnl = MAX_STAKE * df["roi"].sum() - fee_drag * len(df)
    return {
        "n": len(df),
        "n_events": df["event_id"].nunique(),
        "wr": float(df["payout"].mean()),
        "avg_roi": float(df["roi"].mean()),
        "daily_real": net_pnl / N_DAYS * HAIRCUT,
    }


def main():
    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    fight_filter = " AND " + " AND ".join(
        f"LOWER(m.question) NOT LIKE '%{kw}%'" for kw in FIGHT_KW
    )

    # D7b extended (Sports-only subset) — то что D7b ловит в Sports
    # Pretend extreme zone vol_gate (для production это $100K, в canonical $100K) — упростим без gate
    print("=" * 80)
    print("DATASET COMPARISON: D7b extended Sports subset vs D15 best")
    print("=" * 80)

    d7b_sports = fetch(con, """
        AND m.category = 'Sports'
        AND s.yes_price BETWEEN 0.85 AND 0.97
        AND NOT (s.yes_price >= 0.95 AND m.lifetime_volume_usd >= 100000)
    """)
    d7b_sports_excl_fight = d7b_sports[~d7b_sports["question"].str.lower().apply(
        lambda q: any(kw in q for kw in FIGHT_KW)
    )]
    d15_best = fetch(con, f"""
        AND m.category = 'Sports'
        AND m.neg_risk = 0
        AND s.yes_price BETWEEN 0.85 AND 0.92
        {fight_filter}
    """)

    e_d7b_all = calc_pnl(d7b_sports)
    e_d7b_excl = calc_pnl(d7b_sports_excl_fight)
    e_d15 = calc_pnl(d15_best)

    print(f"\n  D7b ext Sports (all): n={e_d7b_all['n']:,} n_ev={e_d7b_all['n_events']:,} "
          f"WR={e_d7b_all['wr']:.1%} ROI={e_d7b_all['avg_roi']:+.1%} daily=${e_d7b_all['daily_real']:+.2f}")
    print(f"  D7b ext Sports excl_fight: n={e_d7b_excl['n']:,} n_ev={e_d7b_excl['n_events']:,} "
          f"WR={e_d7b_excl['wr']:.1%} ROI={e_d7b_excl['avg_roi']:+.1%} daily=${e_d7b_excl['daily_real']:+.2f}")
    print(f"  D15 best: n={e_d15['n']:,} n_ev={e_d15['n_events']:,} "
          f"WR={e_d15['wr']:.1%} ROI={e_d15['avg_roi']:+.1%} daily=${e_d15['daily_real']:+.2f}")

    # Set intersection
    d7b_ids = set(d7b_sports["condition_id"])
    d7b_excl_ids = set(d7b_sports_excl_fight["condition_id"])
    d15_ids = set(d15_best["condition_id"])

    overlap_d7b_d15 = d15_ids & d7b_ids
    only_d15 = d15_ids - d7b_ids
    only_d7b_excl = d7b_excl_ids - d15_ids

    print(f"\n  Set analysis (per condition_id):")
    print(f"    D7b ext Sports: {len(d7b_ids):,} markets")
    print(f"    D7b ext Sports excl_fight: {len(d7b_excl_ids):,} markets")
    print(f"    D15 best: {len(d15_ids):,} markets")
    print(f"    D15 ∩ D7b: {len(overlap_d7b_d15):,} markets ({len(overlap_d7b_d15)/max(len(d15_ids),1):.0%} of D15)")
    print(f"    D15 only (not in D7b): {len(only_d15):,} markets")
    print(f"    D7b ext Sports excl_fight only (not in D15): {len(only_d7b_excl):,} markets")

    # Real D15 incremental alpha
    if only_d15:
        d15_only_df = d15_best[d15_best["condition_id"].isin(only_d15)]
        e_d15_only = calc_pnl(d15_only_df)
        print(f"\n  D15-ONLY incremental: n={e_d15_only['n']:,} "
              f"daily=${e_d15_only['daily_real']:+.2f}")

    # D7b extra over D15 (sports что D15 не ловит)
    if only_d7b_excl:
        d7b_only_df = d7b_sports_excl_fight[d7b_sports_excl_fight["condition_id"].isin(only_d7b_excl)]
        e_d7b_only = calc_pnl(d7b_only_df)
        print(f"  D7b-Sports-excl-fight ONLY (not in D15): n={e_d7b_only['n']:,} "
              f"daily=${e_d7b_only['daily_real']:+.2f}")

    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    if e_d15["daily_real"] <= e_d7b_excl["daily_real"]:
        print("  D15 daily ≤ D7b ext Sports excl_fight daily")
        print("  → D15 не добавляет incremental alpha над D7b extended")
        print("  → KILL D15 (subset of D7b)")
    else:
        print("  D15 > D7b ext Sports excl_fight")
        print("  → D15 имеет incremental alpha")


if __name__ == "__main__":
    main()
