#!/usr/bin/env python3
"""Backtest D27 — DOUBLE_ELITE wallets predict momentum continuation.

Гипотеза (mirror к D26 fade_whale): если smart money (DOUBLE_ELITE class)
делает push в направлении X на market — следующие H часов цена продолжает
двигаться в направлении X с WR > 60%.

DOUBLE_ELITE = 2,023 wallets из wallet_alchemy_classes (top по impact*ROI*volume).

Backtest:
  - Найти трейды DOUBLE_ELITE на marketе, агрегировать в 15-min bucket
  - Filter: push >= $1K (smart money обычно меньше capital чем fade_whales)
  - Hold X hours
  - WR = was направление push'a соответствует movement of price
"""
from __future__ import annotations

import sys
import time

import duckdb

CANONICAL_DIR = "/mnt/data/canonical"
TRADE_BUCKETS = f"{CANONICAL_DIR}/trade_buckets.parquet"
WALLET_CLASSES = f"{CANONICAL_DIR}/wallet_alchemy_classes.parquet"
WALLET_FLOW = f"{CANONICAL_DIR}/wallet_flow_per_market.parquet"


def main() -> int:
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    print("=" * 80)
    print("D27 DOUBLE_ELITE MOMENTUM BACKTEST")
    print("=" * 80)

    # Load DOUBLE_ELITE wallets
    de_count = con.execute(f"""
        SELECT count(*) FROM read_parquet('{WALLET_CLASSES}')
        WHERE class_label = 'double_elite'
    """).fetchone()[0]
    print(f"\nDOUBLE_ELITE wallets: {de_count:,}")

    # Wallet flow per market не имеет per-wallet detail — нужны raw trades.
    # У нас есть только agg trade_buckets. Идея альтернативная:
    # используем wallet_flow_per_market где есть whale_buy_pressure
    # (это net direction всех whale takers в bucket) +
    # trade_buckets с price moves.
    #
    # Гипотеза adapted: высокий whale_buy_pressure → продолжение momentum.
    # whale_buy_pressure = (buy - sell) / (buy + sell) ∈ [-1, 1].

    print("\n--- Whale Buy Pressure → Price Continuation ---")
    print("\nЭтап 1: join trade_buckets + wallet_flow по (cid, hour_bucket)")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE joined AS
        SELECT
            tb.condition_id,
            tb.hour_bucket,
            tb.vwap_yes,
            tb.buy_volume + tb.sell_volume AS total_vol_usd,
            tb.n_distinct_takers,
            wf.whale_taker_volume,
            wf.whale_buy_pressure,  -- [-1, 1]
            wf.whale_taker_count,
            LAG(tb.vwap_yes, 1) OVER w AS prev_vwap,
            LAG(tb.hour_bucket, 1) OVER w AS prev_hour,
            LEAD(tb.vwap_yes, 2) OVER w AS vwap_h2,
            LEAD(tb.vwap_yes, 4) OVER w AS vwap_h4,
            LEAD(tb.vwap_yes, 6) OVER w AS vwap_h6,
            LEAD(tb.hour_bucket, 4) OVER w AS hour_h4
        FROM read_parquet('{TRADE_BUCKETS}') tb
        JOIN read_parquet('{WALLET_FLOW}') wf
            ON tb.condition_id = wf.condition_id
            AND tb.hour_bucket = wf.hour_bucket
        WHERE wf.whale_taker_count >= 3
          AND wf.whale_taker_volume >= 5000
          AND ABS(wf.whale_buy_pressure) >= 0.7  -- strong directional
          AND tb.vwap_yes IS NOT NULL
          AND tb.vwap_yes > 0.05 AND tb.vwap_yes < 0.95
        WINDOW w AS (PARTITION BY tb.condition_id ORDER BY tb.hour_bucket)
    """)

    n_events = con.execute("SELECT count(*) FROM joined").fetchone()[0]
    print(f"  Strong whale-directional buckets: {n_events:,}")

    # ── Шаг 2: Continuation analysis ──────────────────────────────────────────
    for horizon_label, horizon_col, horizon_h in [
        ("2h", "vwap_h2", 2),
        ("4h", "vwap_h4", 4),
        ("6h", "vwap_h6", 6),
    ]:
        print(f"\n--- Horizon {horizon_label} ---")
        results = con.execute(f"""
            WITH eligible AS (
                SELECT
                    *,
                    {horizon_col} AS future_vwap,
                    CASE WHEN whale_buy_pressure > 0 THEN 'WHALE_BUY' ELSE 'WHALE_SELL' END AS direction,
                    CASE
                        WHEN vwap_yes < 0.20 THEN 'low<0.20'
                        WHEN vwap_yes < 0.40 THEN 'mid_low'
                        WHEN vwap_yes < 0.60 THEN 'mid'
                        WHEN vwap_yes < 0.80 THEN 'mid_high'
                        ELSE 'high>0.80'
                    END AS zone
                FROM joined
                WHERE {horizon_col} IS NOT NULL
            )
            SELECT
                direction,
                zone,
                count(*) AS n_events,
                avg(whale_buy_pressure) AS avg_pressure,
                avg(future_vwap - vwap_yes) AS avg_move,
                -- Continuation WR: direction совпадает с future move
                sum(CASE
                    WHEN direction = 'WHALE_BUY' AND future_vwap > vwap_yes THEN 1
                    WHEN direction = 'WHALE_SELL' AND future_vwap < vwap_yes THEN 1
                    ELSE 0
                END)::DOUBLE / count(*) AS continuation_wr,
                -- Среднее движение в сторону direction (наш potential payout)
                avg(CASE
                    WHEN direction = 'WHALE_BUY' THEN future_vwap - vwap_yes
                    WHEN direction = 'WHALE_SELL' THEN vwap_yes - future_vwap
                END) AS avg_directional_pp
            FROM eligible
            GROUP BY 1, 2
            HAVING count(*) >= 100
            ORDER BY continuation_wr DESC
        """).fetchall()

        print(f"  {'dir':12} {'zone':10} {'n':>6} {'press':>7} {'move':>8} {'WR':>6} {'dir_pp':>8}")
        for row in results:
            direction, zone, n_events, avg_pressure, avg_move, wr, dir_pp = row
            print(f"  {direction:12} {zone:10} {n_events:>6} "
                  f"{avg_pressure:>+7.2f} {avg_move:>+8.3f} {wr:>6.1%} {dir_pp:>+8.4f}")

    print(f"\nTotal: {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
