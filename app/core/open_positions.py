"""Open positions snapshot для risk_engine.check_risk().

Phase 1 Stage 2. См. docs/superpowers/plans/2026-05-risk-engine-hardening/02-risk-engine-integration.md.

Тонкий wrapper над execution_orders, фильтрует только live (non-virtual)
позиции с placed/filled status. Возвращает list[OpenPosition] (dataclass
из [[risk-engine]]) для category/event/heat cap checks.

Virtual positions намеренно не включены: они не создают real exposure
(нет реального capital lock). См. detector_lifecycle stage 'live'.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.services.database import fetch
from app.services.risk_engine import OpenPosition

OPEN_POSITIONS_RISK_SQL = """
SELECT eo.market_id,
       m.event_id::text                   AS event_id,
       COALESCE(m.category, 'unknown')    AS category,
       eo.stake::float                    AS stake,
       eo.created_at                      AS created_at
  FROM execution_orders eo
  LEFT JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status IN ('placed', 'filled')
   AND eo.settled_at IS NULL
   AND eo.execution_mode != 'virtual'
"""


def _row_to_open_position(row: dict[str, Any]) -> OpenPosition:
    created = row["created_at"]
    if created is None:
        created = datetime.now(UTC)
    elif created.tzinfo is None:
        created = created.replace(tzinfo=UTC)

    return OpenPosition(
        market_id=row["market_id"],
        event_id=row.get("event_id"),
        category=row.get("category") or "unknown",
        stake=float(row["stake"]),
        created_at=created,
        resolved_at=None,
    )


async def fetch_open_positions_for_risk() -> list[OpenPosition]:
    """Загружает open live positions для risk_engine.check_risk().

    Returns пустой list если БД недоступна или нет open positions.
    Virtual positions исключены (no real capital lock).
    """
    rows = await fetch(OPEN_POSITIONS_RISK_SQL)
    return [_row_to_open_position(dict(r)) for r in rows]
