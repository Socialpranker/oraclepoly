#!/usr/bin/env python3
"""Category × Zone × Time-to-Resolve reversion analysis.

Гипотеза: в определенных категориях (e.g. Politics, Sports) extreme zones
показывают reversion bias через 4-12h, особенно когда time_to_resolve > X.

Используем только: trade_buckets (15.5M) + markets_clean (для category).
"""
from __future__ import annotations

import sys
import time

import duckdb

CANONICAL_DIR = "/mnt/data/canonical"
TRADE_BUCKETS = f"{CANONICAL_DIR}/trade_buckets.parquet"
MARKETS = f"{CANONICAL_DIR}/markets_clean.parquet"


def main() -> int:
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    print("=" * 80)
    print("CATEGORY × ZONE × HORIZON REVERSION ANALYSIS")
    print("=" * 80)

    # Schema markets
    cols = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{MARKETS}') LIMIT 1").fetchall()
    print("\nmarkets_clean columns:")
    for c in cols:
        print(f"  {c[0]}: {c[1]}")

    # ── Step 1: join trade_buckets + markets (resolved only для outcome data) ─
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE joined AS
        SELECT
            tb.condition_id,
            tb.hour_bucket,
            tb.vwap_yes,
            tb.buy_volume + tb.sell_volume AS total_vol_usd,
            tb.n_distinct_takers,
            m.category,
            m.end_date,
            EXTRACT(EPOCH FROM (m.end_date - tb.hour_bucket))/3600.0 AS hours_to_resolve,
            LEAD(tb.vwap_yes, 4) OVER w AS vwap_h4
        FROM read_parquet('{TRADE_BUCKETS}') tb
        JOIN read_parquet('{MARKETS}') m
            ON tb.condition_id = m.condition_id
        WHERE tb.vwap_yes IS NOT NULL
          AND tb.total_volume_usd >= 1000
          AND tb.n_trades >= 5
          AND tb.vwap_yes > 0.02
          AND tb.vwap_yes < 0.98
        WINDOW w AS (PARTITION BY tb.condition_id ORDER BY tb.hour_bucket)
    """)

    n = con.execute("SELECT count(*) FROM joined").fetchone()[0]
    print(f"\nJoined buckets: {n:,}")

    # ── Step 2: agg по category × zone × ttr ─────────────────────────────────
    print("\n--- Category × Zone × Time-To-Resolve (horizon 4h) ---")
    print(f"{'category':18} {'zone':12} {'ttr_h':>8} {'n':>6} {'rev_wr_to_50':>13} {'avg_move':>9}")

    # Reversion to 0.50: для каждой extreme zone проверяем, движется ли цена
    # ближе к 0.50 через 4h. То что нам нужно — fade extreme prices.
    results = con.execute("""
        WITH eligible AS (
            SELECT
                *,
                CASE
                    WHEN vwap_yes < 0.15 THEN 'extreme_low<0.15'
                    WHEN vwap_yes < 0.30 THEN 'low<0.30'
                    WHEN vwap_yes < 0.45 THEN 'mid_low'
                    WHEN vwap_yes < 0.55 THEN 'mid'
                    WHEN vwap_yes < 0.70 THEN 'mid_high'
                    WHEN vwap_yes < 0.85 THEN 'high<0.85'
                    ELSE 'extreme_high>0.85'
                END AS zone,
                CASE
                    WHEN hours_to_resolve < 24 THEN '<24h'
                    WHEN hours_to_resolve < 168 THEN '24-168h'
                    ELSE '>168h'
                END AS ttr_bucket
            FROM joined
            WHERE vwap_h4 IS NOT NULL
              AND hours_to_resolve >= 4  -- need future to exist before resolution
        )
        SELECT
            COALESCE(category, 'NULL') AS category,
            zone,
            ttr_bucket,
            count(*) AS n,
            -- Reversion WR: extreme_low → moved up; extreme_high → moved down
            sum(CASE
                WHEN zone LIKE '%low%' AND vwap_h4 > vwap_yes THEN 1
                WHEN zone LIKE '%high%' AND vwap_h4 < vwap_yes THEN 1
                ELSE 0
            END)::DOUBLE / count(*) AS rev_wr,
            avg(vwap_h4 - vwap_yes) AS avg_move
        FROM eligible
        WHERE zone LIKE '%extreme%'  -- только extreme zones (где edge возможен)
        GROUP BY 1, 2, 3
        HAVING count(*) >= 200
        ORDER BY rev_wr DESC
        LIMIT 30
    """).fetchall()

    for row in results:
        category, zone, ttr, n_events, wr, avg_move = row
        print(f"  {category[:18]:18} {zone:12} {ttr:>8} {n_events:>6} {wr:>13.1%} {avg_move:>9.4f}")

    print(f"\nTotal: {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
