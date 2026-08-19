#!/usr/bin/env python3
"""Backtest price-spike reversion (Transform 4 — внутренняя breakpoint алхимия).

Гипотеза: spike в часовом bucket (Δmax-min ≥ X pp, takers ≥ N, volume ≥ V) →
после spike цена возвращается ближе к pre-spike vwap через H часов.

Edge: купить против движения сразу после spike-bucket.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb

# Локально вызываем через ssh <your-server>; на VPS файл копируется и запускается там
CANONICAL_DIR = "/mnt/data/canonical"
TRADE_BUCKETS = f"{CANONICAL_DIR}/trade_buckets.parquet"


def main() -> int:
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    print("=" * 80)
    print("PRICE SPIKE REVERSION BACKTEST")
    print("=" * 80)

    # ── Шаг 1: построить spike events ─────────────────────────────────────────
    # Часовой bucket с большим диапазоном цен + достаточно активности.
    # Берём только liquid markets (>=$10K total volume в bucket-час), активные
    # такеры >=10 (отфильтровать price flicker одной wallet).
    con.execute("""
        CREATE OR REPLACE TEMP TABLE buckets AS
        SELECT
            condition_id,
            hour_bucket,
            vwap_yes,
            min_yes_price,
            max_yes_price,
            (max_yes_price - min_yes_price) AS price_range,
            buy_volume,
            sell_volume,
            (buy_volume - sell_volume) AS net_direction_usd,
            (buy_volume + sell_volume) AS total_volume_usd,
            n_distinct_takers,
            n_trades
        FROM read_parquet(?)
        WHERE total_volume_usd >= 1000
          AND n_trades >= 5
          AND vwap_yes IS NOT NULL
          AND min_yes_price > 0.02
          AND max_yes_price < 0.98
    """, [TRADE_BUCKETS])

    n_buckets = con.execute("SELECT count(*) FROM buckets").fetchone()[0]
    print(f"\nЭтап 1: {n_buckets:,} clean liquid hour-buckets")

    # ── Шаг 2: spike events с pre + post buckets ──────────────────────────────
    # Для каждого bucket b находим предыдущий b-1 (для baseline vwap)
    # и через H часов b+H (target outcome).
    # Используем LAG/LEAD с partition by condition_id, ordered by hour_bucket.
    for horizon_h in [2, 4, 6]:
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE spike_events_h{horizon_h} AS
            SELECT
                condition_id,
                hour_bucket,
                vwap_yes,
                price_range,
                net_direction_usd,
                total_volume_usd,
                n_distinct_takers,
                LAG(vwap_yes, 1) OVER w AS prev_vwap,
                LAG(hour_bucket, 1) OVER w AS prev_hour,
                LEAD(vwap_yes, {horizon_h}) OVER w AS future_vwap,
                LEAD(hour_bucket, {horizon_h}) OVER w AS future_hour
            FROM buckets
            WINDOW w AS (PARTITION BY condition_id ORDER BY hour_bucket)
        """)

        # ── Шаг 3: filter по гипотезе и compute edge ──────────────────────────
        # Spike conditions: price_range >= 5pp, takers >= 10, volume >= 5K
        # ON one side (net_direction): если net > 0 → UP spike (buy NO afterwards)
        # Цены: prev_vwap должен быть валидным, future_vwap тоже
        # Time gap: prev_hour должен быть ровно -1h, future ровно +H часов
        results = con.execute(f"""
            WITH eligible AS (
                SELECT
                    *,
                    EXTRACT(EPOCH FROM (hour_bucket - prev_hour))/3600.0 AS prev_gap_h,
                    EXTRACT(EPOCH FROM (future_hour - hour_bucket))/3600.0 AS future_gap_h
                FROM spike_events_h{horizon_h}
                WHERE prev_vwap IS NOT NULL
                  AND future_vwap IS NOT NULL
                  AND price_range >= 0.05
                  AND n_distinct_takers >= 10
                  AND total_volume_usd >= 5000
            )
            SELECT
                CASE
                    WHEN net_direction_usd > 0 THEN 'UP_spike'
                    WHEN net_direction_usd < 0 THEN 'DOWN_spike'
                    ELSE 'NEUTRAL'
                END AS spike_dir,
                CASE
                    WHEN prev_vwap < 0.20 THEN 'low<0.20'
                    WHEN prev_vwap < 0.40 THEN 'mid_low'
                    WHEN prev_vwap < 0.60 THEN 'mid'
                    WHEN prev_vwap < 0.80 THEN 'mid_high'
                    ELSE 'high>0.80'
                END AS pre_zone,
                count(*) AS n_events,
                avg(price_range) AS avg_range,
                avg(vwap_yes - prev_vwap) AS avg_spike_move,
                avg(future_vwap - vwap_yes) AS avg_post_move,
                avg((future_vwap - vwap_yes) / NULLIF(vwap_yes - prev_vwap, 0)) AS reversion_ratio,
                -- WR: процент случаев когда future_vwap движется ОБРАТНО спайку
                -- (если UP spike — future < vwap; если DOWN spike — future > vwap)
                sum(CASE
                    WHEN net_direction_usd > 0 AND future_vwap < vwap_yes THEN 1
                    WHEN net_direction_usd < 0 AND future_vwap > vwap_yes THEN 1
                    ELSE 0
                END)::DOUBLE / count(*) AS reversion_wr
            FROM eligible
            WHERE prev_gap_h BETWEEN 0.9 AND 1.1  -- exactly -1h
              AND future_gap_h BETWEEN {horizon_h * 0.9} AND {horizon_h * 1.1}
              AND spike_dir != 'NEUTRAL'
            GROUP BY 1, 2
            HAVING count(*) >= 50
            ORDER BY reversion_wr DESC
        """).fetchall()

        print(f"\n--- Horizon {horizon_h}h ---")
        print(f"  {'dir':12} {'zone':12} {'n':>6} {'rng':>6} {'spike_Δ':>8} {'post_Δ':>8} {'rev_ratio':>10} {'WR':>6}")
        for row in results:
            spike_dir, pre_zone, n_events, avg_range, avg_spike, avg_post, rev_ratio, wr = row
            print(f"  {spike_dir:12} {pre_zone:12} {n_events:>6} "
                  f"{avg_range:>6.3f} {avg_spike:>+8.3f} {avg_post:>+8.3f} "
                  f"{rev_ratio if rev_ratio is not None else 0:>+10.3f} {wr:>6.1%}")

    print(f"\nTotal: {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
