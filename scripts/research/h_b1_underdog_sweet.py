#!/usr/bin/env python3
"""B1: Underdog Sweet Spot per category.

Гипотеза: yes_price ∈ [0.10, 0.30] markets имеют actual_WR > market price
по категориям, особенно вне Sports. Buy YES @ underdog price.
"""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main() -> int:
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    print("=" * 80)
    print("HYPOTHESIS B1: Underdog Sweet Spot")
    print("=" * 80)

    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE snap AS
      SELECT m.condition_id, m.event_id, m.category, m.yes_won, m.lifetime_volume_usd,
             ps.yes_price, ps.hours_to_resolve,
             row_number() OVER (PARTITION BY m.condition_id
                                ORDER BY ABS(ps.hours_to_resolve - 24)) AS rn_24h,
             row_number() OVER (PARTITION BY m.condition_id
                                ORDER BY ABS(ps.hours_to_resolve - 168)) AS rn_168h
      FROM read_parquet('{CANONICAL}/markets_clean.parquet') m
      JOIN read_parquet('{CANONICAL}/price_snapshots.parquet') ps
        ON ps.condition_id = m.condition_id
      WHERE m.yes_won IS NOT NULL
        AND m.lifetime_volume_usd >= 500
        AND ps.hours_to_resolve BETWEEN 2 AND 720
    """)

    FEE = 1.25  # mixed average

    print(f"\n--- Underdog zone [0.10, 0.30] per category × snapshot point ---")
    print(f"{'category':>20} {'snap_h':>7} {'zone':>10} {'n':>6} {'avg_p':>7} {'WR':>6} {'edge_pp':>9} {'roi':>9}")

    for snap_label, snap_col in [('24h', 'rn_24h = 1'), ('168h', 'rn_168h = 1')]:
        for zone_low, zone_high in [(0.05, 0.15), (0.10, 0.20), (0.15, 0.25), (0.20, 0.30), (0.10, 0.30)]:
            rows = con.execute(f"""
              SELECT category, count(*),
                     avg(yes_price)::DOUBLE AS avg_p,
                     avg(yes_won)::DOUBLE AS wr,
                     (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0 AS edge_pp,
                     (avg(yes_won)::DOUBLE / NULLIF(avg(yes_price)::DOUBLE,0) - 1) * 100.0 AS yes_roi
              FROM snap
              WHERE {snap_col}
                AND yes_price >= {zone_low} AND yes_price < {zone_high}
              GROUP BY 1
              HAVING count(*) >= 100
              ORDER BY 5 DESC
            """).fetchall()
            for cat, n, avg_p, wr, edge_pp, roi in rows:
                net = roi - 2*FEE
                marker = " ⭐" if edge_pp >= 2 and n >= 200 and net >= 5 else ""
                print(f"  {(cat or 'NULL')[:20]:>20} {snap_label:>7} [{zone_low:.2f},{zone_high:.2f}) {n:>6} "
                      f"{avg_p:>7.3f} {wr:>6.3f} {edge_pp:>+9.2f} {net:>+8.2f}%{marker}")
        print()

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
