"""Backfill daily_price_snapshots from canonical price_snapshots.parquet.

Canonical stores yes_price at anchor moments {4, 8, 12, 24, 48, 72, 168, 720}h
relative to end_date. We can reconstruct calendar-day history by:

    For each (condition_id, calendar_date) take the **last** snapshot of that day.

This is не точный daily snapshot pipeline, но достаточно для backfill — даёт
~80% coverage of resolved markets за 2024-2026 для rolling-window detectors.

Production cron (daily 00:05 UTC, job daily_price_snapshot) пишет precise
календарные snapshots вперёд.

Storage check before run: each market получает 1 row per active day.
~300K resolved markets × avg 30 active days = ~9M rows ≈ 450 MB.

Usage:
    docker exec oraclepoly-backend python scripts/research/backfill_daily_price_snapshots.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.database import close_db, executemany, init_db  # noqa: E402


CANONICAL_PATH = "/mnt/data/canonical/price_snapshots.parquet"
BATCH_SIZE = 10000


async def main() -> int:
    print(f"Loading {CANONICAL_PATH}")
    t0 = time.time()
    df = pq.read_table(CANONICAL_PATH).to_pandas()
    print(f"  loaded {len(df):,} rows in {time.time() - t0:.1f}s")

    # Convert trade_ts (unix seconds) → date
    df["ts"] = pd.to_datetime(df["trade_ts"], unit="s", utc=True)
    df["snapshot_date"] = df["ts"].dt.date
    print(f"  range: {df['snapshot_date'].min()} → {df['snapshot_date'].max()}")

    # For each (condition_id, snapshot_date) take last snapshot of the day
    df = df.sort_values("ts")
    daily = df.drop_duplicates(subset=["condition_id", "snapshot_date"], keep="last")
    daily = daily[["condition_id", "snapshot_date", "yes_price"]].copy()
    print(f"  unique daily snapshots: {len(daily):,}")
    print(f"  unique markets: {daily['condition_id'].nunique():,}")

    # Insert in batches via executemany — много быстрее single-row execute
    print("\nInserting into daily_price_snapshots (executemany)")
    await init_db()
    try:
        rows = [
            (row["condition_id"], row["snapshot_date"], float(row["yes_price"]))
            for _, row in daily.iterrows()
        ]
        total = len(rows)
        sql = """
        INSERT INTO daily_price_snapshots (condition_id, snapshot_date, yes_price, volume_24h)
        VALUES ($1, $2, $3, NULL)
        ON CONFLICT (condition_id, snapshot_date) DO NOTHING
        """
        t_insert = time.time()
        for i in range(0, total, BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            await executemany(sql, batch)
            done = min(i + BATCH_SIZE, total)
            rate = done / (time.time() - t_insert) if time.time() > t_insert else 0
            eta = (total - done) / rate if rate > 0 else 0
            print(f"  {done:,}/{total:,} ({done * 100 // total}%) — {rate:.0f} rows/s, ETA {eta:.0f}s")
    finally:
        await close_db()

    print("\nBackfill done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
