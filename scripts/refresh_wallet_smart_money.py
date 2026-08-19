#!/usr/bin/env python3
"""Refresh wallet_smart_money table from canonical wallet_performance.parquet.

Запуск: weekly cron на VPS.
  /root/oracle/.venv/bin/python /root/oracle/scripts/refresh_wallet_smart_money.py

Source: /mnt/data/canonical/wallet_performance.parquet (rebuilt nightly via
scripts/research/build_wallet_performance.py + jobs).

Strategy:
  1. Read wallet_performance.parquet (130MB, 2.28M wallets)
  2. Compute top decile ROI threshold among qualified wallets
  3. TRUNCATE + bulk INSERT all wallets с derived flags
     (wallet_smart_money table population)

Dropped from D13 detector quality:
  - Если refresh failed → D13 emission tries DB (graceful — empty result OK)
  - D7b smart money veto → optional (gate inactive если empty)

Idempotent: TRUNCATE + INSERT (replaces old state).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import asyncpg
import duckdb

WALLET_PERF = "/mnt/data/canonical/wallet_performance.parquet"
BATCH_SIZE = 10_000


async def main():
    print("=" * 70, flush=True)
    print("Refresh wallet_smart_money table", flush=True)
    print("=" * 70, flush=True)
    t0 = time.time()

    if not Path(WALLET_PERF).exists():
        print(f"❌ Source not found: {WALLET_PERF}", flush=True)
        return 1

    con = duckdb.connect()

    # Top decile ROI among qualified wallets
    threshold = con.execute(
        f"""
        SELECT QUANTILE_CONT(roi_pct, 0.9)
        FROM read_parquet('{WALLET_PERF}')
        WHERE n_resolved_markets >= 50 AND usd_volume_taker >= 10000
        """
    ).fetchone()[0]
    print(f"  top decile ROI threshold: {threshold:.2f}%", flush=True)

    # Read all wallets с derived top_decile flag
    df = con.execute(
        f"""
        SELECT
          wallet,
          n_resolved_markets,
          usd_volume_taker,
          realized_pnl_usd,
          roi_pct,
          win_rate_market_level,
          bigshot_class,
          is_smart_money,
          last_trade_ts,
          CASE WHEN roi_pct >= {threshold}
                 AND n_resolved_markets >= 50
                 AND usd_volume_taker >= 10000
               THEN TRUE ELSE FALSE END AS is_top_decile
        FROM read_parquet('{WALLET_PERF}')
        """
    ).fetchdf()
    print(f"  wallets to load: {len(df):,}", flush=True)
    print(f"  smart money: {df['is_smart_money'].sum():,}", flush=True)
    print(f"  top decile: {df['is_top_decile'].sum():,}", flush=True)

    # Bulk write to PG via direct asyncpg connection (avoids app config parse)
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not set", flush=True)
        return 1
    conn = await asyncpg.connect(db_url, command_timeout=120)
    try:
        async with conn.transaction():
            await conn.execute("TRUNCATE TABLE public.wallet_smart_money")

            records = list(df.itertuples(index=False, name=None))
            await conn.copy_records_to_table(
                "wallet_smart_money",
                records=records,
                columns=[
                    "wallet",
                    "n_resolved_markets",
                    "usd_volume_taker",
                    "realized_pnl_usd",
                    "roi_pct",
                    "win_rate_market_level",
                    "bigshot_class",
                    "is_smart_money",
                    "last_trade_ts",
                    "is_top_decile",
                ],
            )
    finally:
        await conn.close()

    print(f"\n✅ Loaded {len(df):,} wallets in {time.time() - t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(asyncio.run(main()))
