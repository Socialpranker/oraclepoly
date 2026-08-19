"""pipeline_v2_outcome_sync — hourly :50 (после detector-signals-resolve at :40).

Stage 5 / Two-Stage ML Pipeline.

Заполняет outcome_int / resolved_at в pipeline_v2_shadow_predictions
для detector_signals которые уже resolved.

Single batch UPDATE — atomic.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.database import execute, fetchval

logger = structlog.get_logger()


_SYNC_SQL = """
UPDATE pipeline_v2_shadow_predictions sp
   SET resolved = true,
       outcome_int = CASE
                       WHEN ds.outcome = 'WIN'  THEN 1
                       WHEN ds.outcome = 'LOSS' THEN 0
                       ELSE NULL
                     END,
       resolved_at = ds.resolved_at
  FROM detector_signals ds
 WHERE ds.id = sp.detector_signal_id
   AND ds.resolved = true
   AND sp.resolved = false
"""

_COUNT_PENDING_SQL = "SELECT COUNT(*) FROM pipeline_v2_shadow_predictions WHERE resolved = false"


async def run_pipeline_v2_outcome_sync() -> dict[str, Any]:
    pending_before = await fetchval(_COUNT_PENDING_SQL)
    await execute(_SYNC_SQL)
    pending_after = await fetchval(_COUNT_PENDING_SQL)
    synced = int(pending_before or 0) - int(pending_after or 0)
    logger.info("pipeline_v2_outcome_sync_done", synced=synced, still_pending=int(pending_after or 0))
    return {
        "status": "ok",
        "synced": synced,
        "still_pending": int(pending_after or 0),
    }
