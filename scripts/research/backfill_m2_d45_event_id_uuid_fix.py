"""One-off backfill: M2/D45 писали в detector_signals.event_id внутренний UUID
markets.event_id (FK на events.id) вместо events.polymarket_id (конвенция
d38_scan.py). На resolve это не влияло (resolve джойнит по market_id), но
event-кластеризация аналитики (money-funnel аудиты, event/category_performance
views группируют по event_id) эти строки не группировала.

Сиквел backfill_m2_d45_market_id_uuid_fix.py (PR #683, market_id-болезнь тех же
детекторов) — скрипты независимы, порядок запуска не важен.

Запускать ТОЛЬКО на проде (после деплоя event_id-фиксов d45_chainlink_leadlag_scan.py
и m2_vacuum_hook.py, иначе джобы продолжат плодить UUID-строки):
    docker exec oraclepoly-backend python scripts/research/backfill_m2_d45_event_id_uuid_fix.py [--dry-run] [--detector M2_liquidity_vacuum]

Одна фаза: rewrite event_id UUID → events.polymarket_id (JOIN e.id::text =
ds.event_id). Строки, чей event удалён или без polymarket_id, не трогаем —
скрипт идемпотентен (после rewrite JOIN больше не матчится), можно перезапускать.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.detectors.d45_chainlink_leadlag import DETECTOR_NAME as D45_NAME
from app.detectors.m2_liquidity_vacuum import DETECTOR_NAME as M2_NAME
from app.services.database import close_db, execute, fetchval, init_db

TARGET_DETECTORS = [M2_NAME, D45_NAME]
SQL_TIMEOUT_S = 600.0  # UPDATE с JOIN по events — не упираться в пуловый command_timeout

_COUNT_UUID_ROWS_SQL = """
SELECT count(*)
  FROM detector_signals ds
  JOIN events e ON e.id::text = ds.event_id
 WHERE ds.detector = ANY($1::text[])
   AND e.polymarket_id IS NOT NULL
"""

_REWRITE_EVENT_ID_SQL = """
UPDATE detector_signals ds
   SET event_id = e.polymarket_id
  FROM events e
 WHERE e.id::text = ds.event_id
   AND e.polymarket_id IS NOT NULL
   AND ds.detector = ANY($1::text[])
"""


async def rewrite_event_ids(detectors: list[str], dry_run: bool) -> int:
    """UUID → events.polymarket_id. Возвращает число затронутых строк."""
    candidates = await fetchval(_COUNT_UUID_ROWS_SQL, detectors)
    if dry_run:
        print(f"[DRY] {candidates} строк с UUID event_id → events.polymarket_id")
        return int(candidates or 0)
    status = await execute(_REWRITE_EVENT_ID_SQL, detectors, timeout=SQL_TIMEOUT_S)
    updated = int(status.split()[-1])
    print(f"rewritten {updated} строк (кандидатов было {candidates})")
    return updated


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Не писать в БД, только посчитать")
    parser.add_argument(
        "--detector",
        default=None,
        choices=TARGET_DETECTORS,
        help="Ограничить одним детектором (по умолчанию оба)",
    )
    args = parser.parse_args()
    detectors = [args.detector] if args.detector else TARGET_DETECTORS

    await init_db()
    try:
        await rewrite_event_ids(detectors, dry_run=args.dry_run)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
