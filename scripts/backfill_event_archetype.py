"""One-shot backfill: классифицирует все events через classify_event и UPDATE events.archetype.

Stage 0 / Two-Stage ML Pipeline.

Источник классификатора: research/event_structure_audit.py:classify_event(markets).
Markets группируются по event_id, для каждой группы запускается classifier,
результат пишется в events.archetype.

Usage (на проде через SSH):
    docker exec -i oraclepoly-backend python scripts/backfill_event_archetype.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from typing import Any

import structlog

sys.path.insert(0, "/app")  # for prod docker
sys.path.insert(0, ".")  # for local

from research.event_structure_audit import classify_event  # noqa: E402

from app.services.database import close_db, execute, fetch, init_db  # noqa: E402

logger = structlog.get_logger()


_FETCH_MARKETS_SQL = """
SELECT m.event_id::text   AS event_id,
       m.id::text          AS market_id,
       m.question,
       m.current_price,
       m.resolved,
       m.is_active
  FROM markets m
 WHERE m.event_id IS NOT NULL
 ORDER BY m.event_id, m.id
"""

_UPDATE_SQL = """
UPDATE events SET archetype = $2 WHERE id = $1::uuid
"""


async def run(limit: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    await init_db(min_size=1, max_size=4)
    try:
        rows = await fetch(_FETCH_MARKETS_SQL)
        by_event: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_event[r["event_id"]].append(dict(r))

        if limit is not None:
            keys = list(by_event.keys())[:limit]
            by_event = {k: by_event[k] for k in keys}

        logger.info(
            "backfill_event_archetype_start",
            n_markets=len(rows),
            n_events=len(by_event),
            dry_run=dry_run,
        )

        classified = 0
        updated = 0
        by_archetype: dict[str, int] = defaultdict(int)
        for event_id, markets in by_event.items():
            result = classify_event(markets)
            by_archetype[result.archetype] += 1
            classified += 1
            if not dry_run:
                status = await execute(_UPDATE_SQL, event_id, result.archetype)
                # status выглядит как 'UPDATE 1' — считаем actual updates
                if status and status.startswith("UPDATE "):
                    try:
                        updated += int(status.split()[1])
                    except (IndexError, ValueError):
                        pass

        summary = {
            "classified": classified,
            "updated": updated,
            "by_archetype": dict(by_archetype),
            "dry_run": dry_run,
        }
        logger.info("backfill_event_archetype_done", **summary)
        return summary
    finally:
        await close_db()


def _main() -> None:
    parser = argparse.ArgumentParser(description="Backfill events.archetype via classify_event")
    parser.add_argument("--limit", type=int, default=None, help="Max events (None = all)")
    parser.add_argument("--dry-run", action="store_true", help="Don't UPDATE — just print summary")
    args = parser.parse_args()
    result = asyncio.run(run(limit=args.limit, dry_run=args.dry_run))
    print(result)


if __name__ == "__main__":
    _main()
