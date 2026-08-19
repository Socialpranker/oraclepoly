#!/usr/bin/env python3
"""D28 Crypto Long-Horizon High Fade — second philosopher's stone candidate.

Discovery (from category-zone backtest):
  Crypto markets, yes_price >= 0.85, time_to_resolve >= 168h (1 week)
  → fade YES (buy NO) over 4h hold
  Initial: n=1577, WR 62.8%, avg_move -3.5pp YES (= +3.5pp NO)

Этот скрипт проверяет robustness:
  1. Vol thresholds: $1K, $5K, $10K, $25K
  2. Different horizons: 4h, 6h, 12h
  3. Fees: Crypto = 1.80% per category — net ROI
  4. Subcategories within Crypto
"""
from __future__ import annotations

import sys
import time

import duckdb

CANONICAL_DIR = "/mnt/data/canonical"
TRADE_BUCKETS = f"{CANONICAL_DIR}/trade_buckets.parquet"
MARKETS = f"{CANONICAL_DIR}/markets_clean.parquet"

# Crypto category fee = 1.80% per side (effective 2026-03-30)
CRYPTO_FEE_PCT = 1.80


def main() -> int:
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    print("=" * 80)
    print("D28 CRYPTO HIGH FADE — ROBUSTNESS BACKTEST")
    print("=" * 80)

    # Build base set: Crypto + extreme_high + long horizon
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE base AS
        SELECT
            tb.condition_id,
            tb.hour_bucket,
            tb.vwap_yes,
            tb.buy_volume + tb.sell_volume AS bucket_vol_usd,
            tb.n_distinct_takers,
            m.category,
            m.event_title,
            m.lifetime_volume_usd,
            EXTRACT(EPOCH FROM (m.end_date - tb.hour_bucket))/3600.0 AS hours_to_resolve,
            LEAD(tb.vwap_yes, 4) OVER w  AS vwap_h4,
            LEAD(tb.vwap_yes, 6) OVER w  AS vwap_h6,
            LEAD(tb.vwap_yes, 12) OVER w AS vwap_h12,
            LEAD(tb.vwap_yes, 24) OVER w AS vwap_h24
        FROM read_parquet('{TRADE_BUCKETS}') tb
        JOIN read_parquet('{MARKETS}') m
            ON tb.condition_id = m.condition_id
        WHERE m.category = 'Crypto'
          AND tb.vwap_yes >= 0.85
          AND tb.vwap_yes < 0.98
          AND EXTRACT(EPOCH FROM (m.end_date - tb.hour_bucket))/3600.0 >= 168
          AND tb.total_volume_usd >= 1000
          AND tb.n_trades >= 5
        WINDOW w AS (PARTITION BY tb.condition_id ORDER BY tb.hour_bucket)
    """)

    base_n = con.execute("SELECT count(*) FROM base").fetchone()[0]
    print(f"\nBase set: {base_n:,} buckets (Crypto, yes>=0.85, ttr>=168h)")

    # ── Vol threshold sweep × horizon ─────────────────────────────────────────
    print(f"\n--- Vol threshold × horizon sweep (fee = {CRYPTO_FEE_PCT}% × 2 sides) ---")
    print(f"{'vol_thr':>10} {'horizon':>8} {'n':>6} {'WR':>6} {'avg_move_pp':>12} {'raw_ROI':>9} {'net_ROI':>9}")

    for vol_thr in [1000, 5000, 10000, 25000, 50000]:
        for horizon_col, horizon_label in [
            ("vwap_h4", "4h"),
            ("vwap_h6", "6h"),
            ("vwap_h12", "12h"),
            ("vwap_h24", "24h"),
        ]:
            row = con.execute(f"""
                WITH eligible AS (
                    SELECT vwap_yes, {horizon_col} AS future_vwap
                    FROM base
                    WHERE bucket_vol_usd >= {vol_thr}
                      AND {horizon_col} IS NOT NULL
                )
                SELECT
                    count(*) AS n,
                    sum(CASE WHEN future_vwap < vwap_yes THEN 1 ELSE 0 END)::DOUBLE
                        / NULLIF(count(*), 0) AS wr,
                    avg(future_vwap - vwap_yes) * 100.0 AS avg_move_pp,
                    -- NO entry @ (1 - yes_price). Если future_vwap < vwap_yes →
                    -- NO ушёл вверх → win = (1 - future_vwap) − (1 - vwap_yes)
                    -- = vwap_yes - future_vwap
                    -- ROI на NO entry: win / no_entry_cost = (vwap_yes - future_vwap) / (1 - vwap_yes)
                    avg((vwap_yes - future_vwap) / NULLIF(1.0 - vwap_yes, 0)) * 100.0 AS raw_roi_pct
                FROM eligible
            """).fetchone()
            if row[0] is None or row[0] < 50:
                continue
            n, wr, avg_move, raw_roi = row
            net_roi = raw_roi - 2 * CRYPTO_FEE_PCT  # 2 sides
            print(f"  {vol_thr:>10,d} {horizon_label:>8} {n:>6} {wr:>6.1%} "
                  f"{avg_move:>12.3f} {raw_roi:>9.2f}% {net_roi:>9.2f}%")

    # ── Subcategory разрез ────────────────────────────────────────────────────
    print(f"\n--- Crypto subcategory (event_title heuristic) ---")
    sub_results = con.execute("""
        WITH eligible AS (
            SELECT
                vwap_yes,
                vwap_h4 AS future_vwap,
                CASE
                    WHEN LOWER(event_title) LIKE '%price%' OR LOWER(event_title) LIKE '%hit %' THEN 'Price'
                    WHEN LOWER(event_title) LIKE '%bitcoin%' OR LOWER(event_title) LIKE '%btc%' THEN 'BTC'
                    WHEN LOWER(event_title) LIKE '%ethereum%' OR LOWER(event_title) LIKE '%eth%' THEN 'ETH'
                    WHEN LOWER(event_title) LIKE '%solana%' OR LOWER(event_title) LIKE '%sol%' THEN 'SOL'
                    WHEN LOWER(event_title) LIKE '%doge%' THEN 'DOGE'
                    WHEN LOWER(event_title) LIKE '%memecoin%' OR LOWER(event_title) LIKE '%pepe%' THEN 'MEME'
                    ELSE 'Other_Crypto'
                END AS sub
            FROM base
            WHERE bucket_vol_usd >= 5000
              AND vwap_h4 IS NOT NULL
        )
        SELECT
            sub,
            count(*) AS n,
            sum(CASE WHEN future_vwap < vwap_yes THEN 1 ELSE 0 END)::DOUBLE
                / NULLIF(count(*), 0) AS wr,
            avg((vwap_yes - future_vwap) / NULLIF(1.0 - vwap_yes, 0)) * 100.0 AS raw_roi_pct
        FROM eligible
        GROUP BY 1
        HAVING count(*) >= 30
        ORDER BY wr DESC
    """).fetchall()

    print(f"{'sub':>15} {'n':>6} {'WR':>6} {'raw_ROI':>9} {'net_ROI':>9}")
    for sub, n, wr, raw_roi in sub_results:
        net_roi = raw_roi - 2 * CRYPTO_FEE_PCT
        print(f"  {sub:>15} {n:>6} {wr:>6.1%} {raw_roi:>9.2f}% {net_roi:>9.2f}%")

    # ── Final decision: best config ───────────────────────────────────────────
    print("\n--- Best config sanity check ---")
    best = con.execute("""
        WITH eligible AS (
            SELECT vwap_yes, vwap_h4 AS future_vwap
            FROM base
            WHERE bucket_vol_usd >= 10000
              AND vwap_h4 IS NOT NULL
        )
        SELECT
            count(*) AS n,
            avg(vwap_yes) AS avg_entry,
            avg(vwap_h4) AS avg_exit,
            sum(CASE WHEN future_vwap < vwap_yes THEN 1 ELSE 0 END)::DOUBLE
                / NULLIF(count(*), 0) AS wr,
            avg(vwap_yes - future_vwap) AS avg_pp,
            avg((vwap_yes - future_vwap) / NULLIF(1.0 - vwap_yes, 0)) * 100.0 AS roi_pct
        FROM eligible
    """).fetchone()
    n, avg_entry, avg_exit, wr, avg_pp, roi = best
    print(f"  Vol>=$10K, 4h hold: n={n}, WR={wr:.1%}, avg_pp={avg_pp:.4f}")
    print(f"  Entry YES avg: {avg_entry:.4f} → Exit avg: {avg_exit:.4f}")
    print(f"  Raw ROI: {roi:.2f}% | Net: {roi - 2*CRYPTO_FEE_PCT:.2f}% (fee {CRYPTO_FEE_PCT}% × 2 sides)")

    print(f"\nTotal: {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
