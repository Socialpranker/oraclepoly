"""refresh-tier2-mvs job — hourly :50.

Refresh всех 5 tier-2 materialized views.

См. migrations/20260521090451_create_tier2_materialized_views.sql.
Layer 3 архитектуры (analysis-ready views) — pre-computed aggregates
для interpretation (Jupyter + dashboard + TG digest).

REFRESH CONCURRENTLY требует UNIQUE INDEX на каждом MV (есть).
Не блокирует SELECT'ы (важно для dashboard live UI).
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.database import execute

logger = structlog.get_logger()


TIER2_MVS = [
    "mv_uma_dispute_timeline",
    "mv_manifold_polymarket_pairs",
    "mv_whale_shock_recovery",
    "mv_term_structure_momentum",
    "mv_cohort_stats",
]


async def run_refresh_tier2_mvs() -> dict[str, Any]:
    """Refresh 5 tier-2 MVs одну за одной.

    Не fail на одной MV — продолжаем со следующей. Лучше частичный refresh
    чем полный пропуск часа.
    """
    refreshed = 0
    failed: list[str] = []

    for mv_name in TIER2_MVS:
        try:
            # CONCURRENTLY — без блокировки SELECT'ов. Требует UNIQUE INDEX.
            await execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {mv_name}")
            refreshed += 1
            logger.info("tier2_mv_refreshed", mv=mv_name)
        except Exception as exc:
            failed.append(mv_name)
            logger.warning("tier2_mv_refresh_failed", mv=mv_name, error=str(exc))

    return {
        "refreshed": refreshed,
        "failed": failed,
        "total": len(TIER2_MVS),
    }
