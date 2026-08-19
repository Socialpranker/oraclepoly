#!/usr/bin/env python3
"""G11: Smart-money flow vs price divergence.

Гипотеза: когда smart_money_taker_volume растёт > N x baseline AND price
движется в противоположную сторону (lag) → одно из них wrong. Который?
"""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    # Сначала строим: для каждого market 4h × 72h anchor pairs
    # Разница в smart_money volume → "ускорение flow"
    # И price movement за тот же период
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE divergence AS
      WITH per_anchor AS (
        SELECT
          ws.condition_id,
          ws.anchor_hours,
          ws.smart_money_taker_volume_usd,
          ws.taker_volume_usd_total,
          ws.smart_money_taker_share,
          ps.yes_price
        FROM read_parquet('{CANONICAL}/wallet_score_at_anchor.parquet') ws
        LEFT JOIN read_parquet('{CANONICAL}/price_snapshots.parquet') ps
          ON ps.condition_id = ws.condition_id
          AND ABS(ps.hours_to_resolve - ws.anchor_hours) < 12
        WHERE ws.anchor_hours IN (4, 168)
          AND ps.yes_price IS NOT NULL AND ps.yes_price > 0.02 AND ps.yes_price < 0.98
      ),
      paired AS (
        SELECT
          a4.condition_id,
          a4.smart_money_taker_volume_usd AS sm_vol_4h,
          a168.smart_money_taker_volume_usd AS sm_vol_72h,
          a4.smart_money_taker_share AS sm_share_4h,
          a168.smart_money_taker_share AS sm_share_72h,
          a4.yes_price AS price_4h,
          a168.yes_price AS price_72h,
          -- price change between 72h and 4h (positive = price went up)
          a4.yes_price - a168.yes_price AS price_delta
        FROM per_anchor a4
        JOIN per_anchor a168 ON a168.condition_id = a4.condition_id
        WHERE a4.anchor_hours = 4 AND a168.anchor_hours = 168
      )
      SELECT
        p.*,
        m.yes_won,
        m.category
      FROM paired p
      JOIN read_parquet('{CANONICAL}/markets_clean.parquet') m
        ON m.condition_id = p.condition_id
      WHERE m.yes_won IS NOT NULL
        AND m.lifetime_volume_usd >= 1000
    """)
    n = con.execute("SELECT count(*) FROM divergence").fetchone()[0]
    print(f"base divergence set: {n:,}")
    print()

    FEE = 1.25

    # =================================================================
    # G11.1: smart money volume spike (sm_4h > 3x sm_72h baseline) + price stuck
    # =================================================================
    print("=" * 80)
    print("G11: Smart money volume acceleration (4h vol >> 72h baseline)")
    print("=" * 80)
    print(f"{'sm_4h_mult':>11} {'price_delta':>13} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'best_ROI':>12}")
    for mult_min in [2, 3, 5]:
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

    # =================================================================
    # G12: Smart share grew or shrunk between 72h and 4h
    # =================================================================
    print()
    print("=" * 80)
    print("G12: smart_money_share TREND (between 72h and 4h)")
    print("=" * 80)
    print(f"{'trend':>20} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'best_ROI':>12}")
    for trend_lbl, cond in [
        ("share_grew_>0.2", "sm_share_4h - sm_share_72h > 0.2"),
        ("share_grew_0.1-0.2", "sm_share_4h - sm_share_72h BETWEEN 0.1 AND 0.2"),
        ("share_stable", "ABS(sm_share_4h - sm_share_72h) < 0.05"),
        ("share_shrunk_>0.2", "sm_share_4h - sm_share_72h < -0.2"),
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
        print(f"  {trend_lbl:>20} {n_r:>5} {wr:>7.3f} {avg_p:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    # =================================================================
    # G13: Price moved BIG but smart money DIDN'T follow
    # =================================================================
    print()
    print("=" * 80)
    print("G13: Price move w/o smart money confirmation (retail-driven move)")
    print("=" * 80)
    print(f"{'price_delta':>14} {'sm_share_4h':>13} {'n':>5} {'WR_yes':>7} {'edge_pp':>8} {'best_ROI':>12}")
    for delta_lo, delta_hi, dl in [(-1, -0.10, "down_10pp+"), (0.10, 1, "up_10pp+")]:
        for sm_max in [0.10, 0.20]:
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
            print(f"  {dl:>14} {sm_max:>13.2f} {n_r:>5} {wr:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    print(f"\nElapsed: {time.time()-t0:.1f}s")

if __name__ == "__main__":
    sys.exit(main())
