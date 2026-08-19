#!/usr/bin/env python3
"""D29 NegRisk Sum Arbitrage backtest.

Гипотеза: на neg_risk events когда sum(yes_prices) > 1.0 значимо —
buy NO basket = guaranteed positive expected payout (один winner-loss capped).

Strategy:
  Cost = sum(1 - yes_price_i) = N - sum_yes
  Payout = (N - 1) if single winner OR (N - k) if k winners OR N if cancelled
  Raw profit = sum_yes - 1 (assuming single winner)

Backtest на canonical price_snapshots @ ~168h to resolution.
"""
from __future__ import annotations

import sys
import time

import duckdb

CANONICAL = "/mnt/data/canonical"


def main() -> int:
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    print("=" * 80)
    print("D29 NegRisk Basket Arb Backtest")
    print("=" * 80)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE one_per_market AS
        WITH ms AS (
          SELECT m.condition_id, m.event_id, m.yes_won, m.category,
                 ps.yes_price, ps.hours_to_resolve
            FROM read_parquet('{CANONICAL}/markets_clean.parquet') m
            JOIN read_parquet('{CANONICAL}/price_snapshots.parquet') ps
              ON ps.condition_id = m.condition_id
           WHERE m.neg_risk = true AND m.event_id IS NOT NULL
             AND m.yes_won IS NOT NULL
             AND ps.hours_to_resolve BETWEEN 24 AND 720
        )
        SELECT condition_id, event_id, yes_won, category, yes_price, hours_to_resolve
          FROM (
            SELECT *,
                   row_number() OVER (PARTITION BY event_id, condition_id
                                      ORDER BY ABS(hours_to_resolve - 168)) AS rn
              FROM ms
          )
         WHERE rn = 1
    """)
    n_markets = con.execute("SELECT count(*) FROM one_per_market").fetchone()[0]
    print(f"\nSnapshot per (event, market): {n_markets:,}")

    print(f"\n--- NO basket profitability by threshold ---")
    print(f"{'thresh':>8} {'n_events':>10} {'avg_sum':>9} {'avg_N':>6} "
          f"{'WR_positive':>13} {'avg_ROI':>10} {'med_ROI':>10}")

    for threshold in [1.0, 1.02, 1.05, 1.10, 1.20, 1.50, 2.0]:
        row = con.execute(f"""
            WITH event_summary AS (
                SELECT event_id,
                       count(*) AS n,
                       sum(yes_price) AS sum_yes,
                       sum(yes_won) AS n_winners,
                       (count(*) - sum(yes_price)) AS basket_cost,
                       sum(CASE WHEN yes_won = 0 THEN 1.0 ELSE 0 END) AS no_wins
                  FROM one_per_market
                 GROUP BY event_id
                HAVING count(*) >= 3 AND sum(yes_price) > {threshold}
            )
            SELECT count(*),
                   avg(sum_yes),
                   avg(n),
                   sum(CASE WHEN (no_wins - basket_cost) > 0 THEN 1 ELSE 0 END)::DOUBLE
                       / NULLIF(count(*), 0) AS wr,
                   avg((no_wins - basket_cost) / NULLIF(basket_cost, 0)) * 100.0 AS avg_roi_pct,
                   median((no_wins - basket_cost) / NULLIF(basket_cost, 0)) * 100.0 AS med_roi_pct
              FROM event_summary
        """).fetchone()
        if row[0] == 0:
            continue
        n_events, avg_sum, avg_n, wr, avg_roi, med_roi = row
        # Fees ≈ 2.5% × basket = mostly Crypto/Sports rates ~1.25% × 2 sides
        net_roi = avg_roi - 2.5
        print(f"{threshold:>8.2f} {n_events:>10,} {avg_sum:>9.3f} {avg_n:>6.1f} "
              f"{wr:>13.1%} {avg_roi:>9.2f}% {med_roi:>9.2f}%")

    print(f"\nNote: fees approximate 2.5% drag on basket ROI (1.25% × 2 sides, mixed cats)")
    print(f"Total: {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
