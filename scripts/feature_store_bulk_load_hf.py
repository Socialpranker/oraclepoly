"""One-time bulk load: HF full_hf_enriched.parquet → postgres feature_store.

Run on VPS (parquet is at /mnt/data/polymarket_dataset/).
Idempotent: ON CONFLICT DO UPDATE. Safe to re-run.

Usage:
    python scripts/feature_store_bulk_load_hf.py --dry-run   # count + preview, no writes
    python scripts/feature_store_bulk_load_hf.py             # real load
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC
from pathlib import Path

# Allow standalone execution: add project root to sys.path so `import app.*` works.
sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb
import structlog

from app.models.feature_vector import FeatureVector
from app.repositories.feature_store_repository import FeatureStoreRepository
from app.services.database import close_db, execute, fetchval, init_db

logger = structlog.get_logger()

HF_PARQUET = Path("/mnt/data/polymarket_dataset/full_hf_enriched.parquet")
# asyncpg caps query args at 32767. With 29 cols per row, safe limit is ~1100.
BATCH_SIZE = 1000


async def run(dry_run: bool) -> int:
    if not HF_PARQUET.exists():
        logger.error("hf_parquet_missing", path=str(HF_PARQUET))
        return 1

    await init_db()

    etl_id = await _etl_run_start("feature_store_bulk_load_hf")

    try:
        con = duckdb.connect()
        total_rows = con.execute(f"SELECT COUNT(*) FROM '{HF_PARQUET}'").fetchone()[0]
        logger.info("bulk_load_start", total_rows=total_rows, dry_run=dry_run)

        if dry_run:
            preview = con.execute(
                f"SELECT market_id, side, market_price, category, created_at, win FROM '{HF_PARQUET}' LIMIT 3"
            ).fetchall()
            for row in preview:
                logger.info("preview_row", row=row)
            await _etl_run_finish(etl_id, status="success", rows_processed=total_rows, rows_inserted=0)
            return 0

        repo = FeatureStoreRepository()
        inserted = 0

        offset = 0
        while offset < total_rows:
            batch_df = con.execute(
                f"SELECT market_id, side, entry_price, market_price, category, "
                f"       created_at, end_date, win, whale_tx_count, horizon_days, "
                f"       volume, maker_direction_raw, taker_imbalance_raw "
                f"FROM '{HF_PARQUET}' "
                f"ORDER BY created_at "
                f"LIMIT {BATCH_SIZE} OFFSET {offset}"
            ).fetchdf()

            rows = [_row_to_fv(r) for _, r in batch_df.iterrows()]
            await repo.upsert_batch(rows)
            inserted += len(rows)
            offset += BATCH_SIZE

            if offset % (BATCH_SIZE * 10) == 0:
                logger.info("bulk_load_progress", inserted=inserted, total=total_rows)

        logger.info("bulk_load_done", inserted=inserted)
        await _etl_run_finish(etl_id, status="success", rows_processed=total_rows, rows_inserted=inserted)
        return 0
    except Exception as e:
        logger.exception("bulk_load_failed")
        await _etl_run_finish(etl_id, status="failed", error_message=str(e))
        return 1
    finally:
        await close_db()


def _row_to_fv(r) -> FeatureVector:
    """Convert a parquet row to FeatureVector. HF has no Oracle components → NULL."""
    created_at = r["created_at"]
    if hasattr(created_at, "to_pydatetime"):
        created_at = created_at.to_pydatetime()
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)

    horizon = r.get("horizon_days")
    market_price = float(r["market_price"])
    side = r["side"]
    entry_price = float(r.get("entry_price") or (1 - market_price if side == "NO" else market_price))

    win_val = r.get("win")
    outcome: str | None = None
    win_int: int | None = None
    if win_val is not None and not _is_nan(win_val):
        win_int = int(win_val)
        if side == "YES":
            outcome = "yes" if win_int == 1 else "no"
        else:  # NO
            outcome = "no" if win_int == 1 else "yes"

    return FeatureVector(
        market_id=str(r["market_id"]),
        snapshot_at=created_at,
        side=side,
        category=str(r.get("category") or ""),
        horizon_days=float(horizon) if horizon is not None and not _is_nan(horizon) else None,
        market_price=market_price,
        entry_price=entry_price,
        volume_24h=float(r.get("volume") or 0) or None,
        maker_direction_raw=_nullable_float(r.get("maker_direction_raw")),
        taker_imbalance_raw=_nullable_float(r.get("taker_imbalance_raw")),
        whale_tx_count=int(r.get("whale_tx_count") or 0),
        outcome=outcome,
        win=win_int,
        source="hf",
    )


def _is_nan(x) -> bool:
    try:
        return x != x
    except Exception:
        return False


def _nullable_float(x) -> float | None:
    if x is None or _is_nan(x):
        return None
    return float(x)


async def _etl_run_start(job_name: str) -> int:
    return await fetchval(
        "INSERT INTO etl_runs (job_name, status) VALUES ($1, 'running') RETURNING id",
        job_name,
    )


async def _etl_run_finish(
    run_id: int,
    status: str,
    rows_processed: int = 0,
    rows_inserted: int = 0,
    error_message: str | None = None,
) -> None:
    await execute(
        """
        UPDATE etl_runs
           SET status = $2, finished_at = NOW(),
               rows_processed = $3, rows_inserted = $4, error_message = $5
         WHERE id = $1
        """,
        run_id,
        status,
        rows_processed,
        rows_inserted,
        error_message,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    args = parser.parse_args()
    return asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
