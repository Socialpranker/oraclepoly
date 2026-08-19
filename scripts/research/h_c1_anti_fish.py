#!/usr/bin/env python3
"""C1: Anti-Fish — retail activity in last hours signals misprice.

Идея: для resolved markets с trades в last 12h до resolution
посмотрим если low whale_share + high taker_count = retail pump.
Затем check actual outcome vs price at that moment.

Используем wallet_flow_per_market: retail_taker_avg_size + (total - whale).
"""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'"); con.execute("SET threads=8")

    print("=" * 80)
    print("C1: Anti-Fish (fade retail late activity)")
    print("=" * 80)

    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE joined AS
      SELECT m.condition_id, m.category, m.yes_won,
             tb.hour_bucket, tb.vwap_yes,
             EXTRACT(EPOCH FROM (m.end_date - tb.hour_bucket))/3600.0 AS hours_to_resolve,
             wf.taker_count_total, wf.whale_taker_volume, wf.whale_taker_count,
             tb.total_volume_usd,
             wf.whale_taker_volume / NULLIF(tb.total_volume_usd, 0) AS whale_share,
             tb.buy_volume - tb.sell_volume AS net_buy_usd
      FROM read_parquet('{CANONICAL}/markets_clean.parquet') m
      JOIN read_parquet('{CANONICAL}/trade_buckets.parquet') tb
        ON tb.condition_id = m.condition_id
      JOIN read_parquet('{CANONICAL}/wallet_flow_per_market.parquet') wf
        ON wf.condition_id = tb.condition_id AND wf.hour_bucket = tb.hour_bucket
      WHERE m.yes_won IS NOT NULL
        AND tb.vwap_yes IS NOT NULL
        AND tb.vwap_yes > 0.05 AND tb.vwap_yes < 0.95
        AND tb.total_volume_usd >= 1000
        AND tb.n_trades >= 10
        AND m.end_date IS NOT NULL
        AND EXTRACT(EPOCH FROM (m.end_date - tb.hour_bucket))/3600.0 BETWEEN 1 AND 24
    """)

    n = con.execute("SELECT count(*) FROM joined").fetchone()[0]
    print(f"\nbuckets last 24h before resolve: {n:,}")

    FEE = 1.25
    print(f"\n--- Retail (whale_share < X) net buy direction → fade WR ---")
    print(f"{'whale_share<':>13} {'net_buy_abs>=':>14} {'n':>6} {'fade_WR':>8} {'roi_net':>9}")

    for ws_max in [0.10, 0.20, 0.30, 0.50]:
        for nb_abs_min in [500, 1000, 5000]:
            row = con.execute(f"""
              WITH ev AS (
                SELECT vwap_yes, yes_won, net_buy_usd
                FROM joined
                WHERE whale_share < {ws_max}
                  AND ABS(net_buy_usd) >= {nb_abs_min}
              )
              SELECT count(*),
                     -- Fade WR: net_buy>0 → fade YES (bet NO wins): NO wins = yes_won=0
                     --         net_buy<0 → fade SELL (bet YES wins): YES wins = yes_won=1
                     sum(CASE
                       WHEN net_buy_usd > 0 AND yes_won = 0 THEN 1
                       WHEN net_buy_usd < 0 AND yes_won = 1 THEN 1
                       ELSE 0
                     END)::DOUBLE / NULLIF(count(*), 0),
                     -- ROI: avg ((1-yes_won)/(1-vwap) - 1) if buy NO, (yes_won/vwap - 1) if buy YES
                     avg(CASE
                       WHEN net_buy_usd > 0 THEN (1.0 - yes_won) / NULLIF(1.0 - vwap_yes, 0) - 1.0
                       WHEN net_buy_usd < 0 THEN yes_won / NULLIF(vwap_yes, 0) - 1.0
                       ELSE 0
                     END) * 100.0
              FROM ev
            """).fetchone()
            n, wr, roi = row
            if n is None or n < 100: continue
            net = roi - 2*FEE
            marker = " ⭐" if wr >= 0.55 and n >= 500 and net >= 10 else ""
            print(f"  {ws_max:>13.2f} {nb_abs_min:>14,} {n:>6} {wr:>8.1%} {net:>+8.2f}%{marker}")

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
