"""One-shot backfill: проставляет event_size и archetype в detector_signals
через JOIN на markets/events.

Stage 0 / Two-Stage ML Pipeline.

Logic (см. план docs/superpowers/plans/2026-05-01-two-stage/01-data-foundation.md):
- detector_signals.event_id хранит events.polymarket_id (text, НЕ uuid)
- event_size = COUNT(markets) для events.polymarket_id
- archetype = events.archetype (заполнен предыдущим backfill через classify_event)

Запускать ПОСЛЕ scripts/backfill_event_archetype.py.

Usage:
    docker exec -i oraclepoly-backend python scripts/backfill_detector_signals_event_meta.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import structlog

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from app.services.database import close_db, execute, fetchval, init_db  # noqa: E402

logger = structlog.get_logger()


# Single batch UPDATE: считаем n_markets per event и берём archetype из events.
# event_id в detector_signals — text (polymarket_id), JOIN через events.polymarket_id.
_BACKFILL_SQL = """
WITH event_meta AS (
    SELECT e.polymarket_id            AS event_polymarket_id,
           e.archetype                AS archetype,
           COUNT(m.id)                AS n_markets
      FROM events e
      JOIN markets m ON m.event_id = e.id
     GROUP BY e.polymarket_id, e.archetype
)
UPDATE detector_signals ds
   SET event_size = em.n_markets,
       archetype  = em.archetype
  FROM event_meta em
 WHERE ds.event_id = em.event_polymarket_id
   AND (ds.event_size IS NULL OR ds.archetype IS NULL OR ds.event_size <> em.n_markets OR ds.archetype IS DISTINCT FROM em.archetype)
"""

_COUNT_NULL_SQL = """
SELECT COUNT(*) FROM detector_signals
 WHERE event_size IS NULL OR archetype IS NULL
"""


async def run() -> dict[str, Any]:
    await init_db(min_size=1, max_size=4)
    try:
        before = await fetchval(_COUNT_NULL_SQL)
        logger.info("backfill_detector_meta_start", null_rows=before)

        status = await execute(_BACKFILL_SQL)
        # status выглядит как 'UPDATE N'
        try:
            updated = int(status.split()[1]) if status and status.startswith("UPDATE ") else 0
        except (IndexError, ValueError):
            updated = 0

        after = await fetchval(_COUNT_NULL_SQL)
        result = {
            "rows_with_null_before": int(before or 0),
            "rows_with_null_after": int(after or 0),
            "rows_updated": updated,
        }
        logger.info("backfill_detector_meta_done", **result)
        return result
    finally:
        await close_db()


def _main() -> None:
    parser = argparse.ArgumentParser(description="Backfill detector_signals.event_size + archetype")
    parser.parse_args()
    result = asyncio.run(run())
    print(result)


if __name__ == "__main__":
    _main()
