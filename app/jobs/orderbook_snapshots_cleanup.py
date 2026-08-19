"""orderbook-snapshots-cleanup job — daily 06:35 UTC.

Retention 14 дней для orderbook_event_snapshots (см. data_audit_2026-05-21.md C.6 #5).

Hard cap protection: если table > 2 GB — alert + aggressive cleanup до 1 GB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.services.database import fetchval

logger = structlog.get_logger()


RETENTION_DAYS = 14
HARD_CAP_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
EMERGENCY_TARGET_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB


async def run_orderbook_snapshots_cleanup() -> dict[str, Any]:
    """Daily cleanup orderbook_event_snapshots по retention policy + safety cap.

    Returns:
        {deleted, table_bytes, status}
    """
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)

    deleted = await fetchval(
        "WITH d AS ("
        "    DELETE FROM orderbook_event_snapshots "
        "    WHERE captured_at < $1 "
        "    RETURNING 1"
        ") SELECT count(*) FROM d",
        cutoff,
    )
    deleted = int(deleted or 0)

    table_bytes = await fetchval("SELECT pg_total_relation_size('orderbook_event_snapshots')")
    table_bytes = int(table_bytes or 0)

    status = "ok"

    if table_bytes > HARD_CAP_BYTES:
        # Aggressive cleanup: оставить только последние 7 дней до тех пор пока
        # table не уменьшится. Это safety net против runaway events.
        emergency_cutoff = datetime.now(UTC) - timedelta(days=7)
        extra_deleted = await fetchval(
            "WITH d AS ("
            "    DELETE FROM orderbook_event_snapshots "
            "    WHERE captured_at < $1 "
            "    RETURNING 1"
            ") SELECT count(*) FROM d",
            emergency_cutoff,
        )
        extra_deleted = int(extra_deleted or 0)
        deleted += extra_deleted
        status = "emergency_cleanup"

        # Refresh table size after aggressive prune.
        table_bytes = int(await fetchval("SELECT pg_total_relation_size('orderbook_event_snapshots')") or 0)
        logger.warning(
            "orderbook_snapshots_emergency_cleanup",
            table_bytes=table_bytes,
            extra_deleted=extra_deleted,
        )

    # VACUUM не вызываем (autovacuum сделает). Это снижает risk locking.
    logger.info(
        "orderbook_snapshots_cleanup_done",
        deleted=deleted,
        table_bytes=table_bytes,
        status=status,
    )

    return {
        "deleted": deleted,
        "table_bytes": table_bytes,
        "status": status,
    }
