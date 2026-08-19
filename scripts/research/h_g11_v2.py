#!/usr/bin/env python3
"""G11/12/13 v2: divergence smart_money vs price.

Использует CTE с window function для уникального snapshot per (cid, anchor).
"""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    # Step 1: best price per (cid, anchor_hours) — ближайший snapshot
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE ws_price AS
      WITH per_pair AS (
        SELECT
          ws.condition_id, ws.anchor_hours,
          ws.smart_money_taker_volume_usd, ws.taker_volume_usd_total,
          ws.smart_money_taker_share, ws.top_decile_taker_share,
          ws.taker_pnl_weighted_usd, ws.taker_roi_weighted,
          ps.yes_price,
          ROW_NUMBER() OVER (PARTITION BY ws.condition_id, ws.anchor_hours
                             ORDER BY ABS(ps.hours_to_resolve - ws.anchor_hours)) AS rn
        FROM read_parquet('{CANONICAL}/wallet_score_at_anchor.parquet') ws
        LEFT JOIN read_parquet('{CANONICAL}/price_snapshots.parquet') ps
          ON ps.condition_id = ws.condition_id
          AND ABS(ps.hours_to_resolve - ws.anchor_hours) < 12
        WHERE ws.anchor_hours IN (4, 24, 72)
      )
      SELECT * FROM per_pair WHERE rn = 1 AND yes_price IS NOT NULL
        AND yes_price > 0.02 AND yes_price < 0.98
    """)
    n_ws_price = con.execute("SELECT count(*) FROM ws_price").fetchone()[0]
    print(f"ws_price unique snapshots: {n_ws_price:,}")

    # Step 2: pair 4h vs 72h per condition_id
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE divergence AS
      SELECT
        a4.condition_id,
        a4.smart_money_taker_volume_usd AS sm_vol_4h,
        a72.smart_money_taker_volume_usd AS sm_vol_72h,
        a4.smart_money_taker_share AS sm_share_4h,
        a72.smart_money_taker_share AS sm_share_72h,
        a4.top_decile_taker_share AS td_share_4h,
        a72.top_decile_taker_share AS td_share_72h,
        a4.yes_price AS price_4h,
        a72.yes_price AS price_72h,
        a4.yes_price - a72.yes_price AS price_delta,
        m.yes_won, m.category
      FROM (SELECT * FROM ws_price WHERE anchor_hours = 4) a4
      JOIN (SELECT * FROM ws_price WHERE anchor_hours = 72) a72 ON a72.condition_id = a4.condition_id
      JOIN read_parquet('{CANONICAL}/markets_clean.parquet') m ON m.condition_id = a4.condition_id
      WHERE m.yes_won IS NOT NULL AND m.lifetime_volume_usd >= 1000
    """)
    n = con.execute("SELECT count(*) FROM divergence").fetchone()[0]
    print(f"divergence base: {n:,}")
    print()

    FEE = 1.25

    # G11: smart money volume spike + price stuck
    print("=" * 90)
    print("G11: SM volume 4h >> 72h baseline → outcome direction")
    print("=" * 90)
    print(f"{'sm_4h_mult':>11} {'price_delta':>13} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'best_ROI':>12}")
    for mult_min in [2, 3, 5, 10]:
        for delta_lo, delta_hi, dl in [(-1, -0.05, "down_5pp"), (-0.05, 0.05, "flat"), (0.05, 1, "up_5pp")]:
            row = con.execute(f"""
              SELECT count(*), avg(price_4h)::DOUBLE, avg(yes_won)::DOUBLE,
                     (avg(yes_won)::DOUBLE - avg(price_4h)::DOUBLE) * 100.0,
                     CASE
                       WHEN avg(yes_won)::DOUBLE > avg(price_4h)::DOUBLE
                         THEN (avg(yes_won)::DOUBLE / NULLIF(avg(price_4h)::DOUBLE, 0) - 1) * 100.0
                       ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(price_4h)::DOUBLE, 0) - 1) * 100.0
                     END
              FROM divergence
              WHERE sm_vol_4h > {mult_min} * GREATEST(sm_vol_72h, 100)
                AND price_delta BETWEEN {delta_lo} AND {delta_hi}
                AND sm_vol_4h >= 1000
            """).fetchone()
            n_r, avg_p, wr, edge, roi = row
            if n_r is None or n_r < 100: continue
            net = roi - 2*FEE
            side = "YES" if wr > avg_p else "NO"
            marker = " STAR" if abs(edge) >= 3 and n_r >= 200 and net >= 8 else ""
            print(f"  {mult_min:>11}x {dl:>13} {n_r:>5} {wr:>7.3f} {avg_p:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    # G12: share TREND
    print()
    print("=" * 90)
    print("G12: smart_money_share TREND (4h - 72h)")
    print("=" * 90)
    print(f"{'trend':>22} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'best_ROI':>12}")
    for trend_lbl, cond in [
        ("share_grew_>0.3", "sm_share_4h - sm_share_72h > 0.3"),
        ("share_grew_0.1-0.3", "sm_share_4h - sm_share_72h BETWEEN 0.1 AND 0.3"),
        ("share_stable", "ABS(sm_share_4h - sm_share_72h) < 0.05"),
        ("share_shrunk_0.1-0.3", "sm_share_4h - sm_share_72h BETWEEN -0.3 AND -0.1"),
        ("share_shrunk_>0.3", "sm_share_4h - sm_share_72h < -0.3"),
    ]:
        row = con.execute(f"""
          SELECT count(*), avg(price_4h)::DOUBLE, avg(yes_won)::DOUBLE,
                 (avg(yes_won)::DOUBLE - avg(price_4h)::DOUBLE) * 100.0,
                 CASE
                   WHEN avg(yes_won)::DOUBLE > avg(price_4h)::DOUBLE
                     THEN (avg(yes_won)::DOUBLE / NULLIF(avg(price_4h)::DOUBLE, 0) - 1) * 100.0
                   ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(price_4h)::DOUBLE, 0) - 1) * 100.0
                 END
          FROM divergence WHERE {cond}
        """).fetchone()
        n_r, avg_p, wr, edge, roi = row
        if n_r is None or n_r < 100: continue
        net = roi - 2*FEE
        side = "YES" if wr > avg_p else "NO"
        marker = " STAR" if abs(edge) >= 3 and n_r >= 200 and net >= 8 else ""
        print(f"  {trend_lbl:>22} {n_r:>5} {wr:>7.3f} {avg_p:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    # G13: BIG price move without smart_money confirmation
    print()
    print("=" * 90)
    print("G13: BIG price move (>=10pp) but smart_share LOW (<0.2) → retail-driven mispricing?")
    print("=" * 90)
    print(f"{'price_move':>14} {'sm_share_4h<':>14} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'best_ROI':>12}")
    for delta_lo, delta_hi, dl in [(-1, -0.10, "down_10pp+"), (-0.10, -0.05, "down_5_10pp"),
                                    (0.05, 0.10, "up_5_10pp"), (0.10, 1, "up_10pp+")]:
        for sm_max in [0.10, 0.20, 0.30]:
            row = con.execute(f"""
              SELECT count(*), avg(price_4h)::DOUBLE, avg(yes_won)::DOUBLE,
                     (avg(yes_won)::DOUBLE - avg(price_4h)::DOUBLE) * 100.0,
                     CASE
                       WHEN avg(yes_won)::DOUBLE > avg(price_4h)::DOUBLE
                         THEN (avg(yes_won)::DOUBLE / NULLIF(avg(price_4h)::DOUBLE, 0) - 1) * 100.0
                       ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(price_4h)::DOUBLE, 0) - 1) * 100.0
                     END
              FROM divergence
              WHERE price_delta BETWEEN {delta_lo} AND {delta_hi}
                AND sm_share_4h < {sm_max}
            """).fetchone()
            n_r, avg_p, wr, edge, roi = row
            if n_r is None or n_r < 100: continue
            net = roi - 2*FEE
            side = "YES" if wr > avg_p else "NO"
            marker = " STAR" if abs(edge) >= 3 and n_r >= 200 and net >= 8 else ""
            print(f"  {dl:>14} {sm_max:>14.2f} {n_r:>5} {wr:>7.3f} {avg_p:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
