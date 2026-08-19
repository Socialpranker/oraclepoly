#!/usr/bin/env python3
"""D28 v2 — Crypto rally fade (refinement).

После initial scan: Crypto + yes>=0.85 + ttr>=168h дала только marginal edge.
Делаем deeper segmentation:
  1. Jump magnitude в bucket: |vwap_yes - prev_vwap| >= X
  2. Bucket направление: UP-pressure (buy_volume > sell_volume + threshold)
  3. Multiple yes_price zones: 0.85-0.92, 0.92-0.97
  4. Horizon hold: 24h (где WR 59% обнаружили)

Гипотеза: NOT все extreme_high crypto reverse — а только те, где было
"freshly pumped" в текущий bucket (large UP move + buy pressure).
"""
from __future__ import annotations

import sys
import time

import duckdb

CANONICAL_DIR = "/mnt/data/canonical"
TRADE_BUCKETS = f"{CANONICAL_DIR}/trade_buckets.parquet"
MARKETS = f"{CANONICAL_DIR}/markets_clean.parquet"
WALLET_FLOW = f"{CANONICAL_DIR}/wallet_flow_per_market.parquet"

CRYPTO_FEE_PCT = 1.80


def main() -> int:
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    print("=" * 80)
    print("D28 v2 CRYPTO RALLY FADE — DEEPER SEGMENTATION")
    print("=" * 80)

    # Base: crypto + extreme high + with prev_vwap (jump magnitude)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE base AS
        SELECT
            tb.condition_id,
            tb.hour_bucket,
            tb.vwap_yes,
            tb.buy_volume,
            tb.sell_volume,
            tb.buy_volume + tb.sell_volume AS bucket_vol_usd,
            (tb.buy_volume - tb.sell_volume) / NULLIF(tb.buy_volume + tb.sell_volume, 0) AS net_pressure,
            tb.n_distinct_takers,
            m.category,
            m.event_title,
            EXTRACT(EPOCH FROM (m.end_date - tb.hour_bucket))/3600.0 AS hours_to_resolve,
            LAG(tb.vwap_yes, 1)  OVER w AS prev_vwap,
            LAG(tb.hour_bucket, 1) OVER w AS prev_hour,
            LEAD(tb.vwap_yes, 4)  OVER w AS vwap_h4,
            LEAD(tb.vwap_yes, 12) OVER w AS vwap_h12,
            LEAD(tb.vwap_yes, 24) OVER w AS vwap_h24
        FROM read_parquet('{TRADE_BUCKETS}') tb
        JOIN read_parquet('{MARKETS}') m
            ON tb.condition_id = m.condition_id
        WHERE m.category = 'Crypto'
          AND tb.vwap_yes >= 0.85
          AND tb.vwap_yes < 0.98
          AND EXTRACT(EPOCH FROM (m.end_date - tb.hour_bucket))/3600.0 >= 24
          AND tb.total_volume_usd >= 1000
          AND tb.n_trades >= 5
        WINDOW w AS (PARTITION BY tb.condition_id ORDER BY tb.hour_bucket)
    """)

    base_n = con.execute("SELECT count(*) FROM base").fetchone()[0]
    print(f"\nBase: {base_n:,} Crypto extreme_high buckets")

    # ── Jump magnitude × net pressure × horizon ──────────────────────────────
    print(f"\n--- Jump×Pressure×Horizon (fee {CRYPTO_FEE_PCT}% × 2) ---")
    print(f"{'jump_min':>10} {'pressure_min':>14} {'horizon':>8} {'n':>5} "
          f"{'WR':>6} {'avg_pp':>8} {'raw_ROI':>9} {'net_ROI':>9}")

    for jump_min in [0.0, 0.02, 0.05, 0.08]:
        for pressure_min in [0.0, 0.3, 0.6]:
            for horizon_col, hlabel in [("vwap_h4", "4h"), ("vwap_h12", "12h"), ("vwap_h24", "24h")]:
                row = con.execute(f"""
                    WITH eligible AS (
                        SELECT vwap_yes, {horizon_col} AS future_vwap
                        FROM base
                        WHERE prev_vwap IS NOT NULL
                          AND {horizon_col} IS NOT NULL
                          AND bucket_vol_usd >= 5000
                          AND (vwap_yes - prev_vwap) >= {jump_min}
                          AND net_pressure >= {pressure_min}
                          AND EXTRACT(EPOCH FROM (hour_bucket - prev_hour))/3600.0 BETWEEN 0.9 AND 1.1
                    )
                    SELECT
                        count(*) AS n,
                        sum(CASE WHEN future_vwap < vwap_yes THEN 1 ELSE 0 END)::DOUBLE
                            / NULLIF(count(*), 0) AS wr,
                        avg(vwap_yes - future_vwap) * 100.0 AS avg_pp,
                        avg((vwap_yes - future_vwap) / NULLIF(1.0 - vwap_yes, 0)) * 100.0 AS roi_pct
                    FROM eligible
                """).fetchone()
                if row[0] is None or row[0] < 30:
                    continue
                n, wr, avg_pp, raw_roi = row
                net_roi = raw_roi - 2 * CRYPTO_FEE_PCT
                marker = ""
                if wr >= 0.60 and n >= 50 and net_roi >= 10:
                    marker = " ⭐"
                print(f"  {jump_min:>10.3f} {pressure_min:>14.2f} {hlabel:>8} {n:>5} "
                      f"{wr:>6.1%} {avg_pp:>8.3f} {raw_roi:>9.2f}% {net_roi:>9.2f}%{marker}")

    # ── Best segment deep dive: yes 0.85-0.92 vs 0.92-0.97 ────────────────────
    print(f"\n--- Yes_price subzone × horizon (vol>=$5K) ---")
    print(f"{'zone':>15} {'horizon':>8} {'n':>5} {'WR':>6} {'avg_pp':>8} {'raw_ROI':>9} {'net_ROI':>9}")
    for zone_low, zone_high, label in [(0.85, 0.92, "0.85-0.92"), (0.92, 0.97, "0.92-0.97")]:
        for horizon_col, hlabel in [("vwap_h4", "4h"), ("vwap_h12", "12h"), ("vwap_h24", "24h")]:
            row = con.execute(f"""
                WITH eligible AS (
                    SELECT vwap_yes, {horizon_col} AS future_vwap
                    FROM base
                    WHERE bucket_vol_usd >= 5000
                      AND vwap_yes >= {zone_low} AND vwap_yes < {zone_high}
                      AND {horizon_col} IS NOT NULL
                )
                SELECT
                    count(*) AS n,
                    sum(CASE WHEN future_vwap < vwap_yes THEN 1 ELSE 0 END)::DOUBLE
                        / NULLIF(count(*), 0) AS wr,
                    avg(vwap_yes - future_vwap) * 100.0 AS avg_pp,
                    avg((vwap_yes - future_vwap) / NULLIF(1.0 - vwap_yes, 0)) * 100.0 AS roi_pct
                FROM eligible
            """).fetchone()
            if row[0] is None or row[0] < 20:
                continue
            n, wr, avg_pp, raw_roi = row
            net_roi = raw_roi - 2 * CRYPTO_FEE_PCT
            print(f"  {label:>15} {hlabel:>8} {n:>5} {wr:>6.1%} {avg_pp:>8.3f} "
                  f"{raw_roi:>9.2f}% {net_roi:>9.2f}%")

    print(f"\nTotal: {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
