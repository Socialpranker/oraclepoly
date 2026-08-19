#!/usr/bin/env python3
"""F1: Archetype × TTR edge — used events.archetype column.

In markets_clean у нас category, не archetype. Делаем через "событие vs single market"
flag — neg_risk events с n_yes_winners=1 vs binary markets.
"""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'"); con.execute("SET threads=8")

    print("=" * 80)
    print("F1: Archetype × Stage (binary vs negrisk-multi events)")
    print("=" * 80)

    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE snap AS
      SELECT m.condition_id, m.event_id, m.category, m.yes_won, m.neg_risk,
             ps.yes_price, ps.hours_to_resolve,
             row_number() OVER (PARTITION BY m.condition_id ORDER BY ABS(ps.hours_to_resolve - 168)) AS rn_168,
             row_number() OVER (PARTITION BY m.condition_id ORDER BY ABS(ps.hours_to_resolve - 24)) AS rn_24
      FROM read_parquet('{CANONICAL}/markets_clean.parquet') m
      JOIN read_parquet('{CANONICAL}/price_snapshots.parquet') ps
        ON ps.condition_id = m.condition_id
      WHERE m.yes_won IS NOT NULL AND m.lifetime_volume_usd >= 500
        AND ps.hours_to_resolve BETWEEN 2 AND 720
    """)

    FEE = 1.25
    print(f"\n--- Binary vs NegRisk markets — edge per zone × snapshot ---")
    print(f"{'archetype':>12} {'snap':>5} {'zone':>10} {'n':>5} {'avg_p':>7} {'WR':>6} {'edge':>8} {'side':>3} {'net':>9}")

    for archetype_lbl, archetype_cond in [
        ('binary', 'neg_risk = false'),
        ('negrisk', 'neg_risk = true'),
    ]:
        for snap_lbl, snap_cond in [('24h', 'rn_24 = 1'), ('168h', 'rn_168 = 1')]:
            for zlo, zhi in [(0.05, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.95)]:
                row = con.execute(f"""
                  WITH ev AS (
                    SELECT yes_price, yes_won FROM snap
                    WHERE {archetype_cond} AND {snap_cond}
                      AND yes_price >= {zlo} AND yes_price < {zhi}
                  )
                  SELECT count(*),
                         avg(yes_price)::DOUBLE,
                         avg(yes_won)::DOUBLE,
                         (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0 AS edge
                  FROM ev
                """).fetchone()
                n, avg_p, wr, edge = row
                if n is None or n < 300: continue
                if wr > avg_p:
                    side = 'YES'
                    roi = (wr / max(avg_p, 0.001) - 1) * 100
                else:
                    side = 'NO'
                    roi = ((1-wr) / max(1-avg_p, 0.001) - 1) * 100
                net = roi - 2*FEE
                marker = " ⭐" if abs(edge) >= 3 and n >= 1000 and net >= 10 else ""
                print(f"  {archetype_lbl:>12} {snap_lbl:>5} [{zlo:.2f},{zhi:.2f}) {n:>5} "
                      f"{avg_p:>7.3f} {wr:>6.3f} {edge:>+8.2f} {side:>3} {net:>+8.2f}%{marker}")
            print()

    print(f"Elapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
