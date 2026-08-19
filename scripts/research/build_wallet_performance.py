#!/usr/bin/env python3
"""Build wallet_performance.parquet — per-wallet realized PnL & track record.

Source: /mnt/data/hf_data/quant.parquet (568M trades, 2022-11 → 2026-03)
JOIN with /mnt/data/canonical/markets_clean.parquet (yes_won outcomes)

Per-wallet aggregates:
  - n_resolved_trades, n_resolved_markets, usd_volume_taker
  - realized_pnl_usd, roi_pct, win_rate_market_level
  - bigshot_class (whale/shark/fish/minnow)
  - first_trade_ts / last_trade_ts / days_active
  - is_smart_money flag

PnL formula (verified empirically на Trump 2024 sample):
  BUY:  pnl = token_amount × yes_won - usd_amount
  SELL: pnl = usd_amount - token_amount × yes_won
  Combined: pnl = (yes_won - price) × token_amount × (BUY: +1, SELL: -1)

Filter: taker != maker (no self-cross), yes_won IS NOT NULL.

Memory-efficient: pure SQL pipeline, COPY directly to parquet (no pandas materialization).
Re-write of OOM-killed first version that pulled 2.4M-row DataFrame into Python.

Output: /mnt/data/canonical/wallet_performance.parquet
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb

QUANT = "/mnt/data/hf_data/quant.parquet"
MARKETS = "/mnt/data/canonical/markets_clean.parquet"
OUT = Path("/mnt/data/canonical/wallet_performance.parquet")


def main():
    print("=" * 70, flush=True)
    print("Build wallet_performance.parquet (pure SQL)", flush=True)
    print("=" * 70, flush=True)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("PRAGMA max_temp_directory_size='200GiB'")
    con.execute("SET preserve_insertion_order=false")

    # Single SQL → COPY to parquet, no pandas
    print("\n→ Building wallet performance (single SQL, COPY direct)...", flush=True)

    con.execute(
        f"""
        COPY (
          WITH market_outcomes AS (
            SELECT condition_id, yes_won, lifetime_volume_usd, category
            FROM read_parquet('{MARKETS}')
            WHERE yes_won IS NOT NULL
          ),
          wallet_market AS (
            SELECT
              q.taker AS wallet,
              q.condition_id,
              MIN(q.timestamp) AS first_trade_ts,
              MAX(q.timestamp) AS last_trade_ts,
              COUNT(*) AS n_trades,
              SUM(q.usd_amount) AS volume_usd,
              SUM(
                CASE
                  WHEN q.side = 'BUY' THEN q.token_amount * m.yes_won - q.usd_amount
                  WHEN q.side = 'SELL' THEN q.usd_amount - q.token_amount * m.yes_won
                  ELSE 0
                END
              ) AS pnl_usd
            FROM read_parquet('{QUANT}') q
            INNER JOIN market_outcomes m ON q.condition_id = m.condition_id
            WHERE q.taker != q.maker
            GROUP BY q.taker, q.condition_id
          ),
          wallet_agg AS (
            SELECT
              wallet,
              COUNT(DISTINCT condition_id) AS n_resolved_markets,
              SUM(n_trades) AS n_resolved_trades,
              SUM(volume_usd) AS usd_volume_taker,
              SUM(pnl_usd) AS realized_pnl_usd,
              SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS n_winning_markets,
              MIN(first_trade_ts) AS first_trade_ts,
              MAX(last_trade_ts) AS last_trade_ts,
              SUM(CASE WHEN last_trade_ts >= 1772374527 THEN pnl_usd ELSE 0 END) AS pnl_recent_60d,
              SUM(CASE WHEN last_trade_ts >= 1772374527 THEN volume_usd ELSE 0 END) AS vol_recent_60d
            FROM wallet_market
            GROUP BY wallet
            HAVING SUM(volume_usd) > 0
          )
          SELECT
            wallet,
            n_resolved_markets,
            n_resolved_trades,
            usd_volume_taker,
            realized_pnl_usd,
            n_winning_markets,
            first_trade_ts,
            last_trade_ts,
            pnl_recent_60d,
            vol_recent_60d,
            -- Derived
            CAST(realized_pnl_usd / NULLIF(usd_volume_taker, 0) * 100 AS DOUBLE) AS roi_pct,
            CAST(n_winning_markets * 100.0 / NULLIF(n_resolved_markets, 0) AS DOUBLE) AS win_rate_market_level,
            CASE
              WHEN vol_recent_60d > 0
              THEN CAST(pnl_recent_60d / vol_recent_60d * 100 AS DOUBLE)
              ELSE 0
            END AS roi_recent_60d_pct,
            CAST((last_trade_ts - first_trade_ts) / 86400.0 AS DOUBLE) AS days_active,
            CASE
              WHEN usd_volume_taker >= 100000 THEN 'whale'
              WHEN usd_volume_taker >= 10000 THEN 'shark'
              WHEN usd_volume_taker >= 1000 THEN 'fish'
              ELSE 'minnow'
            END AS bigshot_class,
            CASE
              WHEN n_resolved_markets >= 50
                AND usd_volume_taker >= 10000
                AND realized_pnl_usd / NULLIF(usd_volume_taker, 0) >= 0.05
              THEN TRUE ELSE FALSE
            END AS is_smart_money
          FROM wallet_agg
        ) TO '{OUT}' (FORMAT 'parquet', COMPRESSION 'snappy')
        """
    )

    print(f"\n  → {OUT}", flush=True)
    print(f"  size: {OUT.stat().st_size / 1e6:.1f} MB", flush=True)

    # Summary stats
    print("\n=== Summary ===", flush=True)
    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT}')").fetchone()[0]
    print(f"Total wallets: {n:,}", flush=True)

    n_smart = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{OUT}') WHERE is_smart_money = TRUE"
    ).fetchone()[0]
    print(f"Smart money (≥50 markets, ≥$10K vol, ROI ≥5%): {n_smart:,}", flush=True)

    classes = con.execute(
        f"""
        SELECT bigshot_class, COUNT(*) AS n
        FROM read_parquet('{OUT}')
        GROUP BY bigshot_class
        ORDER BY n DESC
        """
    ).fetchdf()
    print(f"\nBigshot class distribution:\n{classes.to_string()}", flush=True)

    print("\nTop 10 by realized_pnl_usd:", flush=True)
    top_pnl = con.execute(
        f"""
        SELECT
          SUBSTRING(wallet, 1, 12) AS wallet,
          n_resolved_markets,
          ROUND(usd_volume_taker, 0) AS vol_usd,
          ROUND(realized_pnl_usd, 0) AS pnl_usd,
          ROUND(roi_pct, 1) AS roi_pct,
          ROUND(win_rate_market_level, 1) AS wr_pct
        FROM read_parquet('{OUT}')
        ORDER BY realized_pnl_usd DESC
        LIMIT 10
        """
    ).fetchdf()
    print(top_pnl.to_string(), flush=True)

    print("\nTop 10 by ROI (≥100 markets, ≥$50K vol):", flush=True)
    top_roi = con.execute(
        f"""
        SELECT
          SUBSTRING(wallet, 1, 12) AS wallet,
          n_resolved_markets,
          ROUND(usd_volume_taker, 0) AS vol_usd,
          ROUND(realized_pnl_usd, 0) AS pnl_usd,
          ROUND(roi_pct, 1) AS roi_pct,
          ROUND(win_rate_market_level, 1) AS wr_pct
        FROM read_parquet('{OUT}')
        WHERE n_resolved_markets >= 100 AND usd_volume_taker >= 50000
        ORDER BY roi_pct DESC
        LIMIT 10
        """
    ).fetchdf()
    print(top_roi.to_string(), flush=True)

    print(f"\nDone in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
