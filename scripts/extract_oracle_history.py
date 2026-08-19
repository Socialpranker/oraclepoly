#!/usr/bin/env python3
"""Extract resolved oracle_snapshots → parquet (для D7+ backtest).

Phase B.3 (2026-04-30): pipeline V2 detectors используют oracle как **feature**
(не gate). Для D7 OracleZScoreNoBias hypothesis (legacy `dyn_no_*_z-1_*` стратегии)
нужны исторические z_posterior значения per market.

Output: /mnt/data/oracle_history.parquet
  market_id_polymarket, snapshot_at, oracle_score, divergence, z_posterior,
  market_price, volume_24h, whale_tx_count, news_count,
  outcome ('yes'/'no'), final_price, resolved_at, category, end_date

Usage:
    python3 scripts/extract_oracle_history.py
    python3 scripts/extract_oracle_history.py --resolved-only
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("extract_oracle")

# /app/research/output mapped to /root/oracle/research/output (rw from container).
# Это shared volume — host видит файл по /root/oracle/research/output/oracle_history.parquet
OUTPUT_PATH = "/app/research/output/oracle_history.parquet"


_SQL_RESOLVED = """
SELECT
    m.polymarket_id::text         AS market_id,
    os.created_at                  AS snapshot_at,
    os.oracle_score::float8        AS oracle_score,
    os.market_price::float8        AS market_price,
    os.divergence::float8          AS divergence,
    os.z_posterior::float8         AS z_posterior,
    os.volume_24h::float8          AS volume_24h,
    os.whale_tx_count              AS whale_tx_count,
    os.news_count                  AS news_count,
    os.outcome                     AS outcome,
    os.final_price::float8         AS final_price,
    os.resolved_at                 AS resolved_at,
    m.category                     AS category,
    m.end_date                     AS end_date,
    EXTRACT(EPOCH FROM (m.end_date - os.created_at)) / 86400.0 AS horizon_days_at_snapshot
FROM oracle_snapshots os
JOIN markets m ON m.id = os.market_id
WHERE os.outcome IN ('yes', 'no')
  AND os.z_posterior IS NOT NULL
  AND os.divergence IS NOT NULL
ORDER BY os.created_at ASC
"""


_SQL_ALL = _SQL_RESOLVED.replace("WHERE os.outcome IN ('yes', 'no')", "WHERE 1=1")


async def main(resolved_only: bool) -> int:
    from app.services.database import close_db, fetch, init_db

    await init_db()
    try:
        sql = _SQL_RESOLVED if resolved_only else _SQL_ALL
        logger.info("fetching oracle_snapshots (resolved_only=%s)...", resolved_only)
        t0 = time.time()
        rows = await fetch(sql)
        elapsed_fetch = time.time() - t0
        logger.info("fetched %d rows in %.1fs", len(rows), elapsed_fetch)

        if not rows:
            logger.warning("no rows to export")
            return 1

        # Convert to pandas → parquet via duckdb
        import duckdb
        import pandas as pd

        df = pd.DataFrame([dict(r) for r in rows])

        # Cast types
        for col in ("snapshot_at", "resolved_at", "end_date"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")

        logger.info("dtypes:\n%s", df.dtypes)

        # Write via DuckDB (better compression + schema)
        con = duckdb.connect()
        con.register("oracle_df", df)
        con.execute(
            f"""
            COPY oracle_df TO '{OUTPUT_PATH}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )

        # Verify
        info = con.execute(f"SELECT COUNT(*), MIN(snapshot_at), MAX(snapshot_at) FROM read_parquet('{OUTPUT_PATH}')").fetchone()
        logger.info("output: %s, rows=%d, range=[%s, %s]", OUTPUT_PATH, info[0], info[1], info[2])

        size_mb = Path(OUTPUT_PATH).stat().st_size / 1024**2
        logger.info("file size: %.1f MB", size_mb)

        return 0
    finally:
        await close_db()


def _parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--resolved-only", action="store_true", default=True,
                   help="Only export rows with outcome IN ('yes', 'no') [default]")
    p.add_argument("--all", action="store_true",
                   help="Export ALL snapshots including pending")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_cli()
    resolved_only = not args.all
    sys.exit(asyncio.run(main(resolved_only=resolved_only)))
