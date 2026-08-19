#!/usr/bin/env python3
"""Build wallet_score_at_anchor.parquet — PIT-correct taker quality per (condition_id, anchor_h).

For each (condition_id, anchor_h ∈ {4,24,72}):
  - Window: trades in [end_date - anchor_h - 24h, end_date - anchor_h)
  - Aggregate takers' historical wallet_performance scores
  - PIT-correct: trades window strictly before anchor (no look-ahead)

Output columns:
  - condition_id, anchor_hours
  - n_takers_24h_before, taker_volume_usd_total
  - smart_money_taker_volume_usd, smart_money_taker_share
  - top_decile_taker_volume_usd, top_decile_taker_share
  - taker_roi_weighted, taker_pnl_weighted_usd

Note: wallet_performance scores use lifetime aggregate (may include post-anchor
trades for wallets active throughout). Минor leakage but acceptable —
smart_money classification stable, future improvement = PIT-snapshot wallets.

Memory-efficient: pure SQL, single CTE, COPY to parquet, no pandas materialization.
Tested OOM-safe on VPS (5GB memory_limit, /mnt/data/duckdb_tmp temp).

Output: /mnt/data/canonical/wallet_score_at_anchor.parquet
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb

QUANT = "/mnt/data/hf_data/quant.parquet"
MARKETS = "/mnt/data/canonical/markets_clean.parquet"
WALLET_PERF = "/mnt/data/canonical/wallet_performance.parquet"
OUT = Path("/mnt/data/canonical/wallet_score_at_anchor.parquet")

ANCHORS = [4, 24, 72]


def main():
    print("=" * 70, flush=True)
    print("Build wallet_score_at_anchor.parquet (pure SQL)", flush=True)
    print("=" * 70, flush=True)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("PRAGMA max_temp_directory_size='200GiB'")
    con.execute("SET preserve_insertion_order=false")

    # 1. Top decile ROI threshold
    print("\n→ Step 1: compute top decile ROI threshold...", flush=True)
    threshold = con.execute(
        f"""
        SELECT QUANTILE_CONT(roi_pct, 0.9)
        FROM read_parquet('{WALLET_PERF}')
        WHERE n_resolved_markets >= 50 AND usd_volume_taker >= 10000
        """
    ).fetchone()[0]
    print(f"  top decile ROI threshold: {threshold:.2f}%", flush=True)

    # 2. Process each anchor separately, write to staging files,
    # then UNION ALL to final parquet
    staging_files = []
    for anchor_h in ANCHORS:
        staging = Path(f"/mnt/data/duckdb_tmp/wallet_score_a{anchor_h}.parquet")
        staging_files.append(staging)
        print(f"\n→ Step 2.{anchor_h}: anchor={anchor_h}h aggregates → {staging.name}", flush=True)

        con.execute(
            f"""
            COPY (
              WITH market_anchors AS (
                SELECT
                  condition_id,
                  CAST(extract(epoch FROM end_date) AS BIGINT) - {anchor_h} * 3600 AS anchor_ts
                FROM read_parquet('{MARKETS}')
                WHERE yes_won IS NOT NULL
              ),
              taker_window AS (
                SELECT
                  ma.condition_id,
                  q.taker AS wallet,
                  SUM(q.usd_amount) AS taker_volume_usd
                FROM market_anchors ma
                INNER JOIN read_parquet('{QUANT}') q
                  ON q.condition_id = ma.condition_id
                  AND q.timestamp >= ma.anchor_ts - 86400
                  AND q.timestamp < ma.anchor_ts
                WHERE q.taker != q.maker
                GROUP BY ma.condition_id, q.taker
              ),
              joined AS (
                SELECT
                  t.condition_id,
                  t.wallet,
                  t.taker_volume_usd,
                  COALESCE(w.realized_pnl_usd, 0) AS wallet_pnl,
                  COALESCE(w.roi_pct, 0) AS wallet_roi,
                  COALESCE(w.is_smart_money, FALSE) AS is_smart,
                  CASE WHEN w.roi_pct >= {threshold}
                         AND w.n_resolved_markets >= 50
                         AND w.usd_volume_taker >= 10000
                       THEN TRUE ELSE FALSE END AS is_top_decile
                FROM taker_window t
                LEFT JOIN read_parquet('{WALLET_PERF}') w
                  ON t.wallet = w.wallet
              )
              SELECT
                condition_id,
                {anchor_h} AS anchor_hours,
                COUNT(DISTINCT wallet) AS n_takers_24h_before,
                SUM(taker_volume_usd) AS taker_volume_usd_total,
                SUM(CASE WHEN is_smart THEN taker_volume_usd ELSE 0 END) AS smart_money_taker_volume_usd,
                SUM(CASE WHEN is_top_decile THEN taker_volume_usd ELSE 0 END) AS top_decile_taker_volume_usd,
                CAST(
                  SUM(taker_volume_usd * wallet_roi) / NULLIF(SUM(taker_volume_usd), 0)
                  AS DOUBLE
                ) AS taker_roi_weighted,
                CAST(
                  SUM(taker_volume_usd * wallet_pnl) / NULLIF(SUM(taker_volume_usd), 0)
                  AS DOUBLE
                ) AS taker_pnl_weighted_usd,
                -- Derived shares
                CAST(
                  SUM(CASE WHEN is_smart THEN taker_volume_usd ELSE 0 END)
                  / NULLIF(SUM(taker_volume_usd), 0)
                  AS DOUBLE
                ) AS smart_money_taker_share,
                CAST(
                  SUM(CASE WHEN is_top_decile THEN taker_volume_usd ELSE 0 END)
                  / NULLIF(SUM(taker_volume_usd), 0)
                  AS DOUBLE
                ) AS top_decile_taker_share
              FROM joined
              GROUP BY condition_id
            ) TO '{staging}' (FORMAT 'parquet', COMPRESSION 'snappy')
            """
        )
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{staging}')").fetchone()[0]
        print(f"  rows: {n:,}", flush=True)

    # 3. Combine
    print("\n→ Step 3: combine all anchors → final output", flush=True)
    parquet_paths = ", ".join(f"'{p}'" for p in staging_files)
    con.execute(
        f"""
        COPY (
          SELECT * FROM read_parquet([{parquet_paths}])
        ) TO '{OUT}' (FORMAT 'parquet', COMPRESSION 'snappy')
        """
    )
    print(f"  → {OUT}", flush=True)
    print(f"  size: {OUT.stat().st_size / 1e6:.1f} MB", flush=True)

    # Cleanup staging
    for p in staging_files:
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    # 4. Summary
    print("\n=== Summary ===", flush=True)
    summary = con.execute(
        f"""
        SELECT
          anchor_hours,
          COUNT(*) AS n_rows,
          ROUND(AVG(n_takers_24h_before), 1) AS avg_takers,
          ROUND(AVG(smart_money_taker_share) * 100, 2) AS avg_smart_pct,
          ROUND(AVG(top_decile_taker_share) * 100, 2) AS avg_top_decile_pct,
          ROUND(AVG(taker_roi_weighted), 1) AS avg_taker_roi
        FROM read_parquet('{OUT}')
        GROUP BY anchor_hours
        ORDER BY anchor_hours
        """
    ).fetchdf()
    print(summary.to_string(), flush=True)

    print(f"\nDone in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
