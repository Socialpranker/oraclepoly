#!/usr/bin/env python3
"""D1: Volume surge forecast.

Гипотеза: когда bucket volume растёт в N раз vs baseline (rolling 24h avg)
→ это attention spike → direction (по net_buy) предсказывает price 24h forward.
"""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'"); con.execute("SET threads=8")

    print("=" * 80)
    print("D1: Volume Surge Forecast")
    print("=" * 80)

    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE base AS
      SELECT tb.condition_id, tb.hour_bucket, tb.vwap_yes,
             tb.total_volume_usd,
             tb.buy_volume - tb.sell_volume AS net_buy,
             avg(tb.total_volume_usd) OVER (
               PARTITION BY tb.condition_id ORDER BY tb.hour_bucket
               ROWS BETWEEN 25 PRECEDING AND 2 PRECEDING
             ) AS baseline_24h,
             LEAD(tb.vwap_yes, 24) OVER (PARTITION BY tb.condition_id ORDER BY tb.hour_bucket) AS vwap_h24
      FROM read_parquet('{CANONICAL}/trade_buckets.parquet') tb
      WHERE tb.vwap_yes IS NOT NULL AND tb.vwap_yes > 0.05 AND tb.vwap_yes < 0.95
        AND tb.n_trades >= 5
    """)

    FEE = 1.25
    print(f"\n--- Surge multiplier (vol vs baseline_24h) × net_buy direction → 24h forward ---")
    print(f"{'surge_mult>=':>13} {'net_buy_abs>=':>14} {'n':>7} {'cont_WR':>8} {'roi_net':>9}")

    for mult in [2, 3, 5, 10]:
        for nb_min in [500, 1000, 5000]:
            row = con.execute(f"""
              WITH ev AS (
                SELECT vwap_yes, vwap_h24, net_buy
                FROM base
                WHERE baseline_24h IS NOT NULL
                  AND baseline_24h >= 100
                  AND total_volume_usd >= baseline_24h * {mult}
                  AND ABS(net_buy) >= {nb_min}
                  AND vwap_h24 IS NOT NULL
              )
              SELECT count(*),
                     sum(CASE
                       WHEN net_buy > 0 AND vwap_h24 > vwap_yes THEN 1
                       WHEN net_buy < 0 AND vwap_h24 < vwap_yes THEN 1
                       ELSE 0
                     END)::DOUBLE / NULLIF(count(*), 0),
                     avg(CASE
                       WHEN net_buy > 0 THEN (vwap_h24 - vwap_yes) / NULLIF(vwap_yes, 0)
                       WHEN net_buy < 0 THEN (vwap_yes - vwap_h24) / NULLIF(1.0 - vwap_yes, 0)
                       ELSE 0
                     END) * 100.0
              FROM ev
            """).fetchone()
            n, wr, roi = row
            if n is None or n < 100: continue
            net = roi - 2*FEE
            marker = " ⭐" if wr >= 0.55 and n >= 500 and net >= 5 else ""
            print(f"  {mult:>13}x {nb_min:>14,} {n:>7,} {wr:>8.1%} {net:>+8.2f}%{marker}")

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
