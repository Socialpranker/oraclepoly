"""refresh_mv_event_outcome — hourly REFRESH MATERIALIZED VIEW CONCURRENTLY.

Stage 0 / Two-Stage ML Pipeline. View определена в migration
20260501070003_create_mv_event_outcome.sql и используется как ground truth source
для Layer A (Event Selector) — per-event агрегации SUM(pnl)/SUM(stake).

Refresh CONCURRENTLY работает потому что в migration создан UNIQUE INDEX на event_id.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.database import execute

logger = structlog.get_logger()


async def run_refresh_mv_event_outcome() -> dict[str, Any]:
    # CONCURRENTLY требует matview уже populated. Fallback на обычный
    # REFRESH для initial populate (после migration matview пустой).
    try:
        await execute("REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_event_outcome")
        logger.info("mv_event_outcome_refreshed", mode="concurrent")
        return {"status": "ok", "mode": "concurrent"}
    except Exception as e:
        err = str(e)
        if "not populated" in err or "CONCURRENTLY cannot" in err:
            await execute("REFRESH MATERIALIZED VIEW public.mv_event_outcome")
            logger.info("mv_event_outcome_refreshed", mode="initial_populate")
            return {"status": "ok", "mode": "initial_populate"}
        logger.warning("mv_event_outcome_refresh_failed", error=err)
        return {"status": "error", "error": err}
