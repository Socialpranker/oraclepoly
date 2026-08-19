#!/usr/bin/env python3
"""B2: Tail decay — favorites 0.85-0.95 overpriced? Per category × snap."""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'"); con.execute("SET threads=8")

    print("=" * 80)
    print("B2: Favorite Tail Decay (zone 0.85-0.95)")
    print("=" * 80)

    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE snap AS
      SELECT m.condition_id, m.category, m.yes_won, m.lifetime_volume_usd,
             ps.yes_price, ps.hours_to_resolve,
             row_number() OVER (PARTITION BY m.condition_id ORDER BY ABS(ps.hours_to_resolve - 24)) AS rn_24,
             row_number() OVER (PARTITION BY m.condition_id ORDER BY ABS(ps.hours_to_resolve - 168)) AS rn_168,
             row_number() OVER (PARTITION BY m.condition_id ORDER BY ABS(ps.hours_to_resolve - 336)) AS rn_336
      FROM read_parquet('{CANONICAL}/markets_clean.parquet') m
      JOIN read_parquet('{CANONICAL}/price_snapshots.parquet') ps
        ON ps.condition_id = m.condition_id
      WHERE m.yes_won IS NOT NULL AND m.lifetime_volume_usd >= 500
        AND ps.hours_to_resolve BETWEEN 2 AND 720
    """)

    FEE = 1.25
    print(f"\n{'category':>20} {'snap':>5} {'zone':>12} {'n':>6} {'avg_p':>7} {'WR':>6} {'edge_pp':>9} {'NO_roi_net':>11}")
    for snap_lbl, snap_col in [('24h', 'rn_24=1'), ('168h', 'rn_168=1'), ('336h', 'rn_336=1')]:
        for zlo, zhi in [(0.80, 0.90), (0.85, 0.95), (0.90, 0.97)]:
            rows = con.execute(f"""
              SELECT category, count(*),
                     avg(yes_price)::DOUBLE,
                     avg(yes_won)::DOUBLE,
                     (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0 AS edge,
                     ((1.0-avg(yes_won)::DOUBLE)/NULLIF(1.0-avg(yes_price)::DOUBLE,0) - 1) * 100.0 AS no_roi
              FROM snap WHERE {snap_col}
                AND yes_price >= {zlo} AND yes_price < {zhi}
              GROUP BY 1
              HAVING count(*) >= 80
              ORDER BY 6 DESC
            """).fetchall()
            for cat, n, avg_p, wr, edge, no_roi in rows:
                net = no_roi - 2*FEE
                marker = " ⭐" if no_roi >= 15 and n >= 200 else ""
                print(f"  {(cat or 'NULL')[:20]:>20} {snap_lbl:>5} [{zlo:.2f},{zhi:.2f}) "
                      f"{n:>6} {avg_p:>7.3f} {wr:>6.3f} {edge:>+9.2f} {net:>+10.2f}%{marker}")
        print()

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
