#!/usr/bin/env python3
"""Backtest D26 Refined — segmentation analysis.

Прошлый D26 (simple fade-whale push) provel убыточным после fees. Investigate
where edge может скрываться:

  1. По zone (yes_price): extreme [0.85+/0.15-] vs mid [0.30-0.70]
  2. По push size: small [$1K-3K] vs big [$3K-10K] vs huge [$10K+]
  3. По direction: UP-pushes (price ↑) vs DOWN-pushes (price ↓)
  4. По duration: hold 1h vs 4h
  5. Combinations of above

Output: pivot table показывающая net_ROI по сегментам.
"""
from __future__ import annotations

import sys
import time

import duckdb

USERS_PATH = "/mnt/data/hf_data2/alllong/users.parquet"
COHORTS_PATH = "/mnt/data/canonical/wallet_alchemy_classes.parquet"

MIN_PUSH_USD = 1000
HORIZONS = [1, 4]
ROUND_TRIP_FEE_PP = 0.5
SLIPPAGE_PP = 1.5
COST_PP = ROUND_TRIP_FEE_PP + SLIPPAGE_PP


def main() -> int:
    print("=" * 70, flush=True)
    print("Backtest D26 REFINED — segmentation analysis", flush=True)
    print("=" * 70, flush=True)

    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("PRAGMA max_temp_directory_size='200GiB'")

    print("\n[1/3] Building push events (FADE_WHALE only)...", flush=True)
    t0 = time.time()
    sql = f"""
    CREATE OR REPLACE TABLE push_events AS
    SELECT
      u.user AS wallet, u.condition_id,
      DATE_TRUNC('minute', TO_TIMESTAMP(u.timestamp)) AS minute_ts,
      EXTRACT(EPOCH FROM DATE_TRUNC('minute', TO_TIMESTAMP(u.timestamp)))::BIGINT AS minute_unix,
      SUM(u.usd_amount) AS volume_pushed,
      (LAST(u.price ORDER BY u.timestamp) - FIRST(u.price ORDER BY u.timestamp)) AS price_drift,
      AVG(u.price) AS push_price,
      c.class_label
    FROM read_parquet('{USERS_PATH}') u
    JOIN read_parquet('{COHORTS_PATH}') c ON c.wallet = u.user
    WHERE u.role = 'taker' AND u.price BETWEEN 0.01 AND 0.99 AND u.usd_amount > 0
      AND c.class_label = 'fade_whale'
    GROUP BY u.user, u.condition_id, DATE_TRUNC('minute', TO_TIMESTAMP(u.timestamp)),
             c.class_label
    HAVING SUM(u.usd_amount) >= {MIN_PUSH_USD}
       AND ABS(LAST(u.price ORDER BY u.timestamp) - FIRST(u.price ORDER BY u.timestamp)) >= 0.005;
    """
    con.execute(sql)
    n = con.execute("SELECT COUNT(*) FROM push_events").fetchone()[0]
    print(f"  fade_whale pushes: {n:,} | {time.time()-t0:.1f}s", flush=True)

    print("\n[2/3] Compute exit prices for 1h, 4h...", flush=True)
    t0 = time.time()
    for h in HORIZONS:
        h_sec = h * 3600
        win = 600
        sql = f"""
        CREATE OR REPLACE TABLE rev_h{h} AS
        SELECT pe.*, AVG(u.price) AS exit_price,
               CASE
                 WHEN pe.push_price = 0 OR AVG(u.price) IS NULL THEN NULL
                 ELSE SIGN(pe.price_drift) * (pe.push_price - AVG(u.price)) / pe.push_price
               END AS fade_return_raw
        FROM push_events pe
        LEFT JOIN read_parquet('{USERS_PATH}') u
              ON u.condition_id = pe.condition_id
             AND u.timestamp BETWEEN pe.minute_unix + {h_sec} - {win} AND pe.minute_unix + {h_sec} + {win}
        GROUP BY pe.wallet, pe.condition_id, pe.minute_ts, pe.minute_unix, pe.volume_pushed,
                 pe.price_drift, pe.push_price, pe.class_label;
        """
        con.execute(sql)
        valid = con.execute(f"SELECT COUNT(*) FILTER (WHERE fade_return_raw IS NOT NULL) FROM rev_h{h}").fetchone()[0]
        print(f"  h={h}h valid: {valid:,}", flush=True)
    print(f"  Done | {time.time()-t0:.1f}s", flush=True)

    print("\n[3/3] Segmentation analysis...", flush=True)
    print()

    # Segment 1: by zone
    for h in HORIZONS:
        print(f"\n=== HORIZON {h}h ===", flush=True)
        print()
        print(">>> SEGMENT 1: by price zone <<<", flush=True)
        sql = f"""
        SELECT
          CASE
            WHEN push_price < 0.15 THEN 'extreme_low'
            WHEN push_price < 0.30 THEN 'low'
            WHEN push_price < 0.70 THEN 'mid'
            WHEN push_price < 0.85 THEN 'high'
            ELSE 'extreme_high'
          END AS zone,
          COUNT(*) AS n,
          ROUND(100 * COUNT(*) FILTER (WHERE fade_return_raw > 0) / NULLIF(COUNT(*), 0)::numeric, 1) AS wr,
          ROUND(AVG(fade_return_raw)::numeric * 100, 2) AS avg_raw,
          ROUND(MEDIAN(fade_return_raw)::numeric * 100, 2) AS med_raw
        FROM rev_h{h} WHERE fade_return_raw IS NOT NULL
        GROUP BY 1 ORDER BY 1;
        """
        rows = con.execute(sql).fetchall()
        print(f"{'zone':<14} {'n':>8} {'wr':>7} {'avg_raw':>9} {'med_raw':>9} {'net':>9}")
        for r in rows:
            zone, nn, wr, avg, med = r
            net = float(avg) - COST_PP
            marker = "✅" if net > 1 else ("⚠️" if net > 0 else "❌")
            print(f"{zone:<14} {nn:>8,} {wr:>6}% {avg:>8}% {med:>8}% {net:>+8.2f}% {marker}")

        # Segment 2: by push size
        print()
        print(">>> SEGMENT 2: by push size <<<", flush=True)
        sql = f"""
        SELECT
          CASE
            WHEN volume_pushed < 3000 THEN 'small_1k-3k'
            WHEN volume_pushed < 10000 THEN 'big_3k-10k'
            WHEN volume_pushed < 50000 THEN 'huge_10k-50k'
            ELSE 'whale_50k+'
          END AS size_bucket,
          COUNT(*) AS n,
          ROUND(100 * COUNT(*) FILTER (WHERE fade_return_raw > 0) / NULLIF(COUNT(*), 0)::numeric, 1) AS wr,
          ROUND(AVG(fade_return_raw)::numeric * 100, 2) AS avg_raw
        FROM rev_h{h} WHERE fade_return_raw IS NOT NULL
        GROUP BY 1 ORDER BY MIN(volume_pushed);
        """
        rows = con.execute(sql).fetchall()
        print(f"{'size':<14} {'n':>8} {'wr':>7} {'avg_raw':>9} {'net':>9}")
        for r in rows:
            sz, nn, wr, avg = r
            net = float(avg) - COST_PP
            marker = "✅" if net > 1 else ("⚠️" if net > 0 else "❌")
            print(f"{sz:<14} {nn:>8,} {wr:>6}% {avg:>8}% {net:>+8.2f}% {marker}")

        # Segment 3: by direction
        print()
        print(">>> SEGMENT 3: by direction (UP-push vs DOWN-push) <<<", flush=True)
        sql = f"""
        SELECT
          CASE WHEN price_drift > 0 THEN 'UP_push' ELSE 'DOWN_push' END AS dir,
          COUNT(*) AS n,
          ROUND(100 * COUNT(*) FILTER (WHERE fade_return_raw > 0) / NULLIF(COUNT(*), 0)::numeric, 1) AS wr,
          ROUND(AVG(fade_return_raw)::numeric * 100, 2) AS avg_raw
        FROM rev_h{h} WHERE fade_return_raw IS NOT NULL
        GROUP BY 1 ORDER BY 1;
        """
        rows = con.execute(sql).fetchall()
        print(f"{'direction':<14} {'n':>8} {'wr':>7} {'avg_raw':>9} {'net':>9}")
        for r in rows:
            d, nn, wr, avg = r
            net = float(avg) - COST_PP
            marker = "✅" if net > 1 else ("⚠️" if net > 0 else "❌")
            print(f"{d:<14} {nn:>8,} {wr:>6}% {avg:>8}% {net:>+8.2f}% {marker}")

        # Combined: extreme zones × big size
        print()
        print(">>> COMBINED: extreme zones + big push ($3K+) + UP_push <<<", flush=True)
        sql = f"""
        SELECT
          CASE WHEN push_price < 0.15 THEN 'ext_low' WHEN push_price > 0.85 THEN 'ext_high' ELSE 'mid' END AS zone,
          COUNT(*) AS n,
          ROUND(100 * COUNT(*) FILTER (WHERE fade_return_raw > 0) / NULLIF(COUNT(*), 0)::numeric, 1) AS wr,
          ROUND(AVG(fade_return_raw)::numeric * 100, 2) AS avg_raw
        FROM rev_h{h}
        WHERE fade_return_raw IS NOT NULL
          AND volume_pushed >= 3000
          AND price_drift > 0
          AND (push_price < 0.15 OR push_price > 0.85)
        GROUP BY 1 ORDER BY 1;
        """
        rows = con.execute(sql).fetchall()
        print(f"{'zone':<14} {'n':>8} {'wr':>7} {'avg_raw':>9} {'net':>9}")
        for r in rows:
            z, nn, wr, avg = r
            net = float(avg) - COST_PP
            marker = "✅" if net > 1 else ("⚠️" if net > 0 else "❌")
            print(f"{z:<14} {nn:>8,} {wr:>6}% {avg:>8}% {net:>+8.2f}% {marker}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
