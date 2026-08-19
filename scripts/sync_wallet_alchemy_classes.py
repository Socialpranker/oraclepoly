#!/usr/bin/env python3
"""Sync wallet_alchemy_classes.parquet → Postgres wallet_alchemy_classes table.

Source: /mnt/data/canonical/wallet_alchemy_classes.parquet (12,138 wallets:
2023 double_elite + 10,115 fade_whale).

Usage: weekly cron OR ad-hoc после rebuild canonical wallet impact.

Idempotent: TRUNCATE + bulk INSERT.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import asyncpg
import duckdb

CLASSES_PATH = "/mnt/data/canonical/wallet_alchemy_classes.parquet"
def _get_db_dsn() -> str:
    """Resolve DB DSN from env at connect time (fail-fast, no hardcoded secret)."""
    try:
        return os.environ["DB_DSN"]
    except KeyError as exc:
        raise SystemExit(
            "DB_DSN env var must be set "
            "(in prod cron it is exported as $DATABASE_URL inside the container; "
            "for manual runs export DB_DSN before invoking this script)"
        ) from exc

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS wallet_alchemy_classes (
    wallet                  text NOT NULL PRIMARY KEY,
    class_label             text NOT NULL,
    n_samples               int  NOT NULL,
    n_markets               int  NOT NULL,
    total_volume_taken      numeric(14,2) NOT NULL,
    median_impact_per_1k    numeric(8,5)  NOT NULL,
    roi_pct_lifetime        numeric(8,2)  NOT NULL,
    refreshed_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wallet_alchemy_class ON wallet_alchemy_classes(class_label);
"""

TRUNCATE_SQL = "TRUNCATE TABLE wallet_alchemy_classes"


async def main() -> int:
    print("Sync wallet_alchemy_classes → Postgres", flush=True)
    t0 = time.time()

    if not Path(CLASSES_PATH).exists():
        print(f"❌ Source not found: {CLASSES_PATH}", flush=True)
        return 1

    con = duckdb.connect()
    df = con.execute(f"""
        SELECT wallet, class_label, n_samples, n_markets,
               total_volume_taken, median_impact_per_1k, roi_pct_lifetime
        FROM read_parquet('{CLASSES_PATH}')
    """).fetchdf()
    print(f"  Loaded {len(df):,} wallets from parquet", flush=True)
    print(f"  Classes: {df['class_label'].value_counts().to_dict()}", flush=True)

    pg = await asyncpg.connect(_get_db_dsn(), command_timeout=120)
    try:
        async with pg.transaction():
            await pg.execute(CREATE_TABLE_SQL)
            await pg.execute(TRUNCATE_SQL)
            records = list(df.itertuples(index=False, name=None))
            await pg.copy_records_to_table(
                "wallet_alchemy_classes",
                records=records,
                columns=[
                    "wallet", "class_label", "n_samples", "n_markets",
                    "total_volume_taken", "median_impact_per_1k", "roi_pct_lifetime",
                ],
            )
        print(f"\n✅ Loaded {len(df):,} wallets in {time.time()-t0:.1f}s", flush=True)
        return 0
    finally:
        await pg.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(asyncio.run(main()))
