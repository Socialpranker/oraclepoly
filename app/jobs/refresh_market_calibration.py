"""refresh-market-calibration job — hourly :55.

Refresh mv_market_calibration (калибровка рынка на нейтральной выборке).
См. migrations/20260613140000_create_mv_market_calibration.sql.

REFRESH CONCURRENTLY требует UNIQUE INDEX (есть: idx_mv_market_calibration_key).
Не блокирует SELECT'ы (важно для dashboard).
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.database import execute

logger = structlog.get_logger()

_MV_NAME = "mv_market_calibration"


async def run_refresh_market_calibration() -> dict[str, Any]:
    """Refresh mv_market_calibration. Не fail — репортит результат."""
    refreshed = 0
    failed: list[str] = []
    try:
        await execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {_MV_NAME}")
        refreshed = 1
        logger.info("market_calibration_mv_refreshed", mv=_MV_NAME)
    except Exception as exc:
        failed.append(_MV_NAME)
        logger.warning("market_calibration_mv_refresh_failed", mv=_MV_NAME, error=str(exc))
    return {"refreshed": refreshed, "failed": failed}
