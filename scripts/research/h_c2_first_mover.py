#!/usr/bin/env python3
"""C2: First-mover smart trades.

Когда whale_taker_volume >= $5K появляется впервые в markete с zero prior
whale activity — это "opening big bet". Проверим WR на следующие 24-72h.
"""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'"); con.execute("SET threads=8")

    print("=" * 80)
    print("C2: First-Mover Smart Trades")
    print("=" * 80)

    # First whale event per market (>$5K in single hour bucket, but prior cumulative=0)
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE wf AS
      SELECT
        wf.condition_id, wf.hour_bucket,
        wf.whale_taker_volume, wf.whale_buy_pressure,
        wf.whale_taker_count,
        tb.vwap_yes, tb.buy_volume - tb.sell_volume AS net_buy,
        sum(wf.whale_taker_volume) OVER (PARTITION BY wf.condition_id ORDER BY wf.hour_bucket) AS cum_whale_vol,
        LEAD(tb.vwap_yes, 24) OVER (PARTITION BY wf.condition_id ORDER BY wf.hour_bucket) AS vwap_h24,
        LEAD(tb.vwap_yes, 72) OVER (PARTITION BY wf.condition_id ORDER BY wf.hour_bucket) AS vwap_h72,
        m.yes_won, m.category
      FROM read_parquet('{CANONICAL}/wallet_flow_per_market.parquet') wf
      JOIN read_parquet('{CANONICAL}/trade_buckets.parquet') tb
        ON tb.condition_id = wf.condition_id AND tb.hour_bucket = wf.hour_bucket
      JOIN read_parquet('{CANONICAL}/markets_clean.parquet') m
        ON m.condition_id = wf.condition_id
      WHERE tb.vwap_yes IS NOT NULL AND tb.vwap_yes > 0.05 AND tb.vwap_yes < 0.95
        AND m.yes_won IS NOT NULL
    """)

    FEE = 1.25
    print(f"\n--- 'First whale bet' (whale_vol >= X, before that cum = 0) → 24h/72h ---")
    print(f"{'whale_min':>10} {'pressure_min':>13} {'horizon':>8} {'n':>6} {'cont_WR':>8} {'roi_net':>9}")
    for whale_min in [3000, 5000, 10000, 25000]:
        for pressure in [0.3, 0.6, 0.8]:
            for horizon, col in [(24, 'vwap_h24'), (72, 'vwap_h72')]:
                row = con.execute(f"""
                  WITH first_events AS (
                    SELECT *,
                           cum_whale_vol - whale_taker_volume AS prior_cum
                    FROM wf
                    WHERE whale_taker_volume >= {whale_min}
                      AND ABS(whale_buy_pressure) >= {pressure}
                      AND {col} IS NOT NULL
                  )
                  SELECT count(*),
                         -- cont WR: цена двинулась в сторону first whale push
                         sum(CASE
                           WHEN whale_buy_pressure > 0 AND {col} > vwap_yes THEN 1
                           WHEN whale_buy_pressure < 0 AND {col} < vwap_yes THEN 1
                           ELSE 0
                         END)::DOUBLE / NULLIF(count(*), 0),
                         -- ROI следуя first whale direction
                         avg(CASE
                           WHEN whale_buy_pressure > 0 THEN ({col} - vwap_yes) / NULLIF(vwap_yes, 0)
                           WHEN whale_buy_pressure < 0 THEN (vwap_yes - {col}) / NULLIF(1.0 - vwap_yes, 0)
                           ELSE 0
                         END) * 100.0
                  FROM first_events
                  WHERE prior_cum = 0
                """).fetchone()
                n, wr, roi = row
                if n is None or n < 100: continue
                net = roi - 2*FEE
                marker = " ⭐" if wr >= 0.55 and n >= 300 and net >= 10 else ""
                print(f"  {whale_min:>10,} {pressure:>13.2f} {horizon:>6}h {n:>6,} {wr:>8.1%} {net:>+8.2f}%{marker}")
        print()

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
