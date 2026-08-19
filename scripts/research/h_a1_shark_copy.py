#!/usr/bin/env python3
"""Hypothesis A1 — Shark Copy Trade.

Гипотеза:
  Когда в hour bucket (markets:condition_id) присутствуют smart-money
  takers с consistent направлением (whale_buy_pressure > 0.6 либо < -0.6)
  и достаточный whale_taker_volume — направление подтверждается ценой
  через 24h с WR > 60%.

Используем canonical wallet_flow_per_market.parquet (whale_buy_pressure,
whale_taker_volume) — это уже агрегаты smart-money flow.
Сравниваем с LEAD(vwap_yes) через 24 buckets.
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
    print("HYPOTHESIS A1: Shark Copy Trade")
    print("=" * 80)

    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE joined AS
      SELECT
        tb.condition_id, tb.hour_bucket,
        tb.vwap_yes, tb.total_volume_usd,
        wf.whale_taker_volume, wf.whale_taker_count, wf.whale_buy_pressure,
        m.category, m.lifetime_volume_usd,
        LEAD(tb.vwap_yes, 4)  OVER w AS vwap_h4,
        LEAD(tb.vwap_yes, 24) OVER w AS vwap_h24,
        LEAD(tb.vwap_yes, 48) OVER w AS vwap_h48,
        LEAD(tb.hour_bucket, 24) OVER w AS hour_h24
      FROM read_parquet('{CANONICAL}/trade_buckets.parquet') tb
      JOIN read_parquet('{CANONICAL}/wallet_flow_per_market.parquet') wf
        ON tb.condition_id = wf.condition_id AND tb.hour_bucket = wf.hour_bucket
      JOIN read_parquet('{CANONICAL}/markets_clean.parquet') m
        ON m.condition_id = tb.condition_id
      WHERE tb.vwap_yes IS NOT NULL
        AND tb.vwap_yes > 0.05 AND tb.vwap_yes < 0.95
      WINDOW w AS (PARTITION BY tb.condition_id ORDER BY tb.hour_bucket)
    """)

    print("\n--- Сегментация: whale_volume × pressure → forward direction WR ---")
    print(f"{'whale_vol_min':>12} {'pressure_min':>13} {'horizon':>8} {'n':>6} "
          f"{'cont_WR':>8} {'avg_pp':>9} {'roi_pct':>9}")

    FEE = 1.25  # mixed average
    for whale_vol_min in [1000, 5000, 10000, 25000, 50000]:
        for pressure_min in [0.3, 0.6, 0.8]:
            for horizon, col in [(4, 'vwap_h4'), (24, 'vwap_h24'), (48, 'vwap_h48')]:
                row = con.execute(f"""
                  WITH eligible AS (
                    SELECT vwap_yes, {col} AS future_vwap, whale_buy_pressure
                    FROM joined
                    WHERE whale_taker_volume >= {whale_vol_min}
                      AND ABS(whale_buy_pressure) >= {pressure_min}
                      AND {col} IS NOT NULL
                      AND whale_taker_count >= 3
                  )
                  SELECT
                    count(*) AS n,
                    -- continuation WR: цена шла В сторону pressure
                    sum(CASE
                      WHEN whale_buy_pressure > 0 AND future_vwap > vwap_yes THEN 1
                      WHEN whale_buy_pressure < 0 AND future_vwap < vwap_yes THEN 1
                      ELSE 0
                    END)::DOUBLE / NULLIF(count(*), 0) AS cont_wr,
                    -- avg directional move (в сторону pressure)
                    avg(CASE
                      WHEN whale_buy_pressure > 0 THEN future_vwap - vwap_yes
                      WHEN whale_buy_pressure < 0 THEN vwap_yes - future_vwap
                      ELSE 0
                    END) * 100.0 AS avg_pp,
                    -- ROI на entry side direction. Если pressure>0, buy YES at vwap_yes:
                    -- payout per dollar = (future_vwap - vwap_yes) / vwap_yes
                    avg(CASE
                      WHEN whale_buy_pressure > 0 THEN (future_vwap - vwap_yes) / NULLIF(vwap_yes, 0)
                      WHEN whale_buy_pressure < 0 THEN (vwap_yes - future_vwap) / NULLIF(1.0 - vwap_yes, 0)
                      ELSE 0
                    END) * 100.0 AS roi_pct
                  FROM eligible
                """).fetchone()
                n, cwr, avg_pp, roi = row
                if n is None or n < 100: continue
                net = roi - 2*FEE
                marker = " ⭐" if cwr >= 0.6 and n >= 500 and net >= 5 else ""
                print(f"  {whale_vol_min:>12,} {pressure_min:>13.2f} {horizon:>6}h {n:>6,} "
                      f"{cwr:>8.1%} {avg_pp:>+9.3f} {net:>+9.2f}%{marker}")
        print()

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
