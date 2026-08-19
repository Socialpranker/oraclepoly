#!/usr/bin/env python3
"""D2: Dormant wake-up — market quiet 48h+, then sudden cluster."""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'"); con.execute("SET threads=8")

    print("=" * 80)
    print("D2: Dormant Wake-up")
    print("=" * 80)

    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE base AS
      SELECT tb.condition_id, tb.hour_bucket, tb.vwap_yes,
             tb.total_volume_usd, tb.n_trades,
             tb.buy_volume - tb.sell_volume AS net_buy,
             sum(tb.total_volume_usd) OVER (
               PARTITION BY tb.condition_id ORDER BY tb.hour_bucket
               ROWS BETWEEN 48 PRECEDING AND 2 PRECEDING
             ) AS prior_48h_vol,
             LEAD(tb.vwap_yes, 4)  OVER (PARTITION BY tb.condition_id ORDER BY tb.hour_bucket) AS vwap_h4,
             LEAD(tb.vwap_yes, 24) OVER (PARTITION BY tb.condition_id ORDER BY tb.hour_bucket) AS vwap_h24
      FROM read_parquet('{CANONICAL}/trade_buckets.parquet') tb
      WHERE tb.vwap_yes IS NOT NULL AND tb.vwap_yes > 0.05 AND tb.vwap_yes < 0.95
    """)

    FEE = 1.25
    print(f"\n--- Dormant (prior_48h <= X) → cluster (current vol >= Y) → forward ---")
    print(f"{'prior_max':>10} {'curr_min':>10} {'horizon':>8} {'n':>6} {'cont_WR':>8} {'roi_net':>9}")
    for prior_max in [100, 500, 1000]:
        for curr_min in [2000, 5000, 10000]:
            for horizon, col in [(4, 'vwap_h4'), (24, 'vwap_h24')]:
                row = con.execute(f"""
                  WITH ev AS (
                    SELECT vwap_yes, {col} AS future_vwap, net_buy
                    FROM base
                    WHERE prior_48h_vol IS NOT NULL
                      AND prior_48h_vol <= {prior_max}
                      AND total_volume_usd >= {curr_min}
                      AND ABS(net_buy) >= 200
                      AND n_trades >= 5
                      AND {col} IS NOT NULL
                  )
                  SELECT count(*),
                         sum(CASE
                           WHEN net_buy > 0 AND future_vwap > vwap_yes THEN 1
                           WHEN net_buy < 0 AND future_vwap < vwap_yes THEN 1
                           ELSE 0
                         END)::DOUBLE / NULLIF(count(*), 0),
                         avg(CASE
                           WHEN net_buy > 0 THEN (future_vwap - vwap_yes) / NULLIF(vwap_yes, 0)
                           WHEN net_buy < 0 THEN (vwap_yes - future_vwap) / NULLIF(1.0 - vwap_yes, 0)
                           ELSE 0
                         END) * 100.0
                  FROM ev
                """).fetchone()
                n, wr, roi = row
                if n is None or n < 50: continue
                net = roi - 2*FEE
                marker = " ⭐" if wr >= 0.55 and n >= 200 and net >= 10 else ""
                print(f"  {prior_max:>10,} {curr_min:>10,} {horizon:>6}h {n:>6,} {wr:>8.1%} {net:>+8.2f}%{marker}")
        print()

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
