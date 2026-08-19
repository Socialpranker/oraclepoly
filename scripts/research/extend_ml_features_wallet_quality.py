#!/usr/bin/env python3
"""Extend ml_features.parquet с wallet quality columns.

Source:
  - /mnt/data/canonical/ml_features.parquet (existing 55 PIT-correct columns)
  - /mnt/data/canonical/wallet_score_at_anchor.parquet (new wallet quality features)

Output: /mnt/data/canonical/ml_features_v2.parquet (extended schema)

New columns added:
  - n_takers_24h_before
  - taker_volume_usd_total_24h
  - smart_money_taker_volume_usd
  - smart_money_taker_share
  - top_decile_taker_volume_usd
  - top_decile_taker_share
  - taker_roi_weighted
  - taker_pnl_weighted_usd

These are PIT-correct: aggregated only from trades в [anchor - 24h, anchor].

Usage: для re-run LGB feature importance с new dimensions to see if wallet
quality surfaces tradeable edge that pure flow signals не уловили.
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb

ML_FEATURES = "/mnt/data/canonical/ml_features.parquet"
WALLET_SCORE = "/mnt/data/canonical/wallet_score_at_anchor.parquet"
OUT = Path("/mnt/data/canonical/ml_features_v2.parquet")


def main():
    print("=" * 70)
    print("Extend ml_features with wallet quality columns")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='8GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("PRAGMA max_temp_directory_size='35GiB'")
    con.execute("SET preserve_insertion_order=false")

    # LEFT JOIN — preserve all existing rows even если wallet_score absent
    con.execute(
        f"""
        COPY (
          SELECT
            ml.*,
            COALESCE(ws.n_takers_24h_before, 0) AS n_takers_24h_before,
            COALESCE(ws.taker_volume_usd_total, 0) AS taker_volume_usd_total_24h,
            COALESCE(ws.smart_money_taker_volume_usd, 0) AS smart_money_taker_volume_usd,
            COALESCE(ws.smart_money_taker_share, 0) AS smart_money_taker_share,
            COALESCE(ws.top_decile_taker_volume_usd, 0) AS top_decile_taker_volume_usd,
            COALESCE(ws.top_decile_taker_share, 0) AS top_decile_taker_share,
            COALESCE(ws.taker_roi_weighted, 0) AS taker_roi_weighted_24h,
            COALESCE(ws.taker_pnl_weighted_usd, 0) AS taker_pnl_weighted_usd_24h
          FROM read_parquet('{ML_FEATURES}') ml
          LEFT JOIN read_parquet('{WALLET_SCORE}') ws
            ON ml.condition_id = ws.condition_id
           AND ml.anchor_hours = ws.anchor_hours
        ) TO '{OUT}' (FORMAT 'parquet', COMPRESSION 'snappy')
        """
    )

    print(f"  → {OUT}")
    print(f"  size: {OUT.stat().st_size / 1e6:.1f} MB")

    # Verify
    print("\n=== Verification ===")
    n_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT}')").fetchone()[0]
    print(f"Total rows: {n_rows:,}")

    n_with_score = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{OUT}') WHERE n_takers_24h_before > 0"
    ).fetchone()[0]
    print(f"Rows with wallet score (≥1 taker pre-anchor): {n_with_score:,} ({n_with_score / n_rows * 100:.1f}%)")

    # Schema
    print("\nSchema:")
    schema = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{OUT}') LIMIT 1").fetchdf()
    print(f"  Total columns: {len(schema)}")
    new_cols = [c for c in schema["column_name"] if c in {
        "n_takers_24h_before", "taker_volume_usd_total_24h",
        "smart_money_taker_volume_usd", "smart_money_taker_share",
        "top_decile_taker_volume_usd", "top_decile_taker_share",
        "taker_roi_weighted_24h", "taker_pnl_weighted_usd_24h",
    }]
    print(f"  New wallet quality columns: {len(new_cols)}")
    for c in new_cols:
        print(f"    - {c}")

    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
