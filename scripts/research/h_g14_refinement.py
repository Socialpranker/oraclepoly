#!/usr/bin/env python3
"""G14: Refinement of "retail rally fade" — narrow filters per category, zone.

Found: up_10pp+ + sm_share<0.10 → NO ROI net +9.9% (n=1521).
Now check: per category, per starting price zone, larger price moves, etc.
"""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"


def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ws_price AS
      WITH per_pair AS (
        SELECT
          ws.condition_id, ws.anchor_hours,
          ws.smart_money_taker_volume_usd, ws.smart_money_taker_share,
          ws.top_decile_taker_share, ws.taker_volume_usd_total,
          ws.taker_pnl_weighted_usd, ws.taker_roi_weighted,
          ps.yes_price,
          ROW_NUMBER() OVER (PARTITION BY ws.condition_id, ws.anchor_hours
                             ORDER BY ABS(ps.hours_to_resolve - ws.anchor_hours)) AS rn
        FROM read_parquet('{CANONICAL}/wallet_score_at_anchor.parquet') ws
        LEFT JOIN read_parquet('{CANONICAL}/price_snapshots.parquet') ps
          ON ps.condition_id = ws.condition_id
          AND ABS(ps.hours_to_resolve - ws.anchor_hours) < 12
        WHERE ws.anchor_hours IN (4, 72)
      )
      SELECT * FROM per_pair WHERE rn = 1 AND yes_price IS NOT NULL
        AND yes_price > 0.02 AND yes_price < 0.98
    """)

    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE rally_fade AS
      SELECT
        a4.condition_id,
        a4.smart_money_taker_share AS sm_share_4h,
        a4.top_decile_taker_share AS td_share_4h,
        a4.taker_volume_usd_total AS vol_4h,
        a4.yes_price AS price_4h,
        a72.yes_price AS price_72h,
        a4.yes_price - a72.yes_price AS price_delta,
        m.yes_won, m.category, m.lifetime_volume_usd, m.event_id
      FROM (SELECT * FROM ws_price WHERE anchor_hours = 4) a4
      JOIN (SELECT * FROM ws_price WHERE anchor_hours = 72) a72 ON a72.condition_id = a4.condition_id
      JOIN read_parquet('{CANONICAL}/markets_clean.parquet') m ON m.condition_id = a4.condition_id
      WHERE m.yes_won IS NOT NULL AND m.lifetime_volume_usd >= 1000
        AND a4.yes_price - a72.yes_price >= 0.10  -- price ralled >=10pp
        AND a4.smart_money_taker_share < 0.10  -- smart money NOT participating
    """)
    n = con.execute("SELECT count(*) FROM rally_fade").fetchone()[0]
    print(f"rally_fade base: {n:,}")
    print()

    FEE = 1.25

    # Per category breakdown
    print("=" * 80)
    print("G14 Rally Fade per category")
    print("=" * 80)
    print(f"{'category':>20} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'NO_roi':>10}")
    rows = con.execute("""
      SELECT category, count(*), avg(price_4h)::DOUBLE, avg(yes_won)::DOUBLE,
             (avg(yes_won)::DOUBLE - avg(price_4h)::DOUBLE) * 100.0,
             ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(price_4h)::DOUBLE, 0) - 1) * 100.0
      FROM rally_fade GROUP BY 1 HAVING count(*) >= 50 ORDER BY 6 DESC
    """).fetchall()
    for cat, n_r, avg_p, wr, edge, roi in rows:
        net = roi - 2*FEE
        marker = " STAR" if edge <= -3 and n_r >= 100 and net >= 8 else ""
        print(f"  {(cat or 'NULL')[:20]:>20} {n_r:>5} {wr:>7.3f} {avg_p:>7.3f} {edge:>+8.2f} {net:>+9.2f}%{marker}")

    # Per starting zone
    print()
    print("=" * 80)
    print("G14 by starting (price_72h) zone")
    print("=" * 80)
    print(f"{'zone_72h':>12} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'NO_roi':>10}")
    for zlo, zhi in [(0.05, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.95)]:
        row = con.execute(f"""
          SELECT count(*), avg(price_4h)::DOUBLE, avg(yes_won)::DOUBLE,
                 (avg(yes_won)::DOUBLE - avg(price_4h)::DOUBLE) * 100.0,
                 ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(price_4h)::DOUBLE, 0) - 1) * 100.0
          FROM rally_fade WHERE price_72h >= {zlo} AND price_72h < {zhi}
        """).fetchone()
        n_r, avg_p, wr, edge, roi = row
        if n_r is None or n_r < 50: continue
        net = roi - 2*FEE
        marker = " STAR" if edge <= -3 and n_r >= 100 and net >= 8 else ""
        print(f"  [{zlo:.2f},{zhi:.2f}) {n_r:>5} {wr:>7.3f} {avg_p:>7.3f} {edge:>+8.2f} {net:>+9.2f}%{marker}")

    # Big rallies (>15pp, >20pp, >30pp)
    print()
    print("=" * 80)
    print("G14 by rally magnitude")
    print("=" * 80)
    print(f"{'rally_min':>10} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'NO_roi':>10}")
    for rally_min in [0.10, 0.15, 0.20, 0.30]:
        row = con.execute(f"""
          SELECT count(*), avg(price_4h)::DOUBLE, avg(yes_won)::DOUBLE,
                 (avg(yes_won)::DOUBLE - avg(price_4h)::DOUBLE) * 100.0,
                 ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(price_4h)::DOUBLE, 0) - 1) * 100.0
          FROM rally_fade WHERE price_delta >= {rally_min}
        """).fetchone()
        n_r, avg_p, wr, edge, roi = row
        if n_r is None or n_r < 50: continue
        net = roi - 2*FEE
        marker = " STAR" if edge <= -3 and n_r >= 100 and net >= 8 else ""
        print(f"  {rally_min:>10.2f} {n_r:>5} {wr:>7.3f} {avg_p:>7.3f} {edge:>+8.2f} {net:>+9.2f}%{marker}")

    # Volume floor
    print()
    print("=" * 80)
    print("G14 by lifetime_volume_usd floor (liquidity)")
    print("=" * 80)
    print(f"{'vol_min':>12} {'n':>5} {'WR_yes':>7} {'edge_pp':>8} {'NO_roi':>10}")
    for vol_min in [1000, 5000, 10000, 50000]:
        row = con.execute(f"""
          SELECT count(*), avg(price_4h)::DOUBLE, avg(yes_won)::DOUBLE,
                 (avg(yes_won)::DOUBLE - avg(price_4h)::DOUBLE) * 100.0,
                 ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(price_4h)::DOUBLE, 0) - 1) * 100.0
          FROM rally_fade WHERE lifetime_volume_usd >= {vol_min}
        """).fetchone()
        n_r, avg_p, wr, edge, roi = row
        if n_r is None or n_r < 50: continue
        net = roi - 2*FEE
        print(f"  >=${vol_min:>8,} {n_r:>5} {wr:>7.3f} {edge:>+8.2f} {net:>+9.2f}%")

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
