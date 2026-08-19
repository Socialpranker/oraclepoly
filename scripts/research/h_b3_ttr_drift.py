#!/usr/bin/env python3
"""B3: TTR calibration drift — как edge меняется с time-to-resolve.

Для zone × category смотрим edge на разных hours_to_resolve.
Гипотеза: какие-то zones имеют growing edge с TTR (long-term mispricing).
"""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'"); con.execute("SET threads=8")

    print("=" * 80)
    print("B3: TTR Calibration Drift")
    print("=" * 80)

    # Each snapshot per (cid, ttr_bucket): closest available
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE snap AS
      SELECT m.condition_id, m.category, m.yes_won, m.lifetime_volume_usd,
             ps.yes_price, ps.hours_to_resolve
      FROM read_parquet('{CANONICAL}/markets_clean.parquet') m
      JOIN read_parquet('{CANONICAL}/price_snapshots.parquet') ps
        ON ps.condition_id = m.condition_id
      WHERE m.yes_won IS NOT NULL
        AND m.lifetime_volume_usd >= 500
        AND ps.hours_to_resolve BETWEEN 2 AND 720
    """)

    # Compute edge (actual_WR - market_price) per (category, ttr_bucket, zone_bucket)
    print(f"\n--- Edge_pp by zone × category × TTR (n>=200) ---")
    print(f"  Все TTR в одном table; rows>=200")

    print(f"\n{'category':>15} {'zone':>10} {'ttr':>10} {'n':>5} {'avg_p':>6} {'WR':>6} {'edge_pp':>8} {'best_side_ROI':>14}")
    rows = con.execute("""
      WITH bucketed AS (
        SELECT category,
               CASE
                 WHEN yes_price < 0.10 THEN '0.00-0.10'
                 WHEN yes_price < 0.25 THEN '0.10-0.25'
                 WHEN yes_price < 0.45 THEN '0.25-0.45'
                 WHEN yes_price < 0.55 THEN '0.45-0.55'
                 WHEN yes_price < 0.75 THEN '0.55-0.75'
                 WHEN yes_price < 0.90 THEN '0.75-0.90'
                 ELSE '0.90-1.00'
               END AS zone,
               CASE
                 WHEN hours_to_resolve < 12 THEN '0-12h'
                 WHEN hours_to_resolve < 48 THEN '12-48h'
                 WHEN hours_to_resolve < 168 THEN '48-168h'
                 WHEN hours_to_resolve < 336 THEN '168-336h'
                 ELSE '>336h'
               END AS ttr,
               yes_price, yes_won
        FROM snap
      )
      SELECT category, zone, ttr,
             count(*),
             avg(yes_price)::DOUBLE AS avg_p,
             avg(yes_won)::DOUBLE AS wr,
             (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0 AS edge_pp,
             -- best side roi: if WR > price → YES, else NO
             CASE
               WHEN avg(yes_won)::DOUBLE > avg(yes_price)::DOUBLE
                 THEN (avg(yes_won)::DOUBLE / NULLIF(avg(yes_price)::DOUBLE, 0) - 1) * 100.0
               ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(yes_price)::DOUBLE, 0) - 1) * 100.0
             END AS best_roi
      FROM bucketed
      GROUP BY 1,2,3
      HAVING count(*) >= 200
      ORDER BY ABS(edge_pp) DESC
      LIMIT 50
    """).fetchall()
    FEE = 1.25
    for cat, zone, ttr, n, avg_p, wr, edge, roi in rows:
        side = "YES" if wr > avg_p else "NO"
        net = roi - 2*FEE
        marker = " ⭐" if abs(edge) >= 5 and n >= 500 and net >= 15 else ""
        print(f"  {(cat or 'NULL')[:15]:>15} {zone:>10} {ttr:>10} {n:>5} "
              f"{avg_p:>6.3f} {wr:>6.3f} {edge:>+8.2f} {side}_{net:>+9.2f}%{marker}")

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
