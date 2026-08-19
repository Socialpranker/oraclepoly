"""One-off backfill: detector_signals.realized_roi_net для уже resolved сигналов.

Запускать ТОЛЬКО на проде:
    docker exec oraclepoly-backend python scripts/research/backfill_realized_roi_net.py [--dry-run]

После миграции 20260501215911_detector_signals_add_source_and_realized_roi_net.sql
все resolved сигналы имеют outcome+realized_roi (gross), но realized_roi_net=NULL.

EV-regressor cron (ml-market-picker-ev-fit) фильтрует по
`realized_roi_net IS NOT NULL` — без backfill он не найдёт training data.

Формула совпадает с app.jobs.detector_signals_resolve._realized_roi_net_pct
(SSoT). Чанки по 10K. EXPIRED/VOID → 0.0 (не None, чтобы EV-fit не считал
их пропусками).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow execution from repo root: PYTHONPATH=.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.jobs.detector_signals_resolve import _realized_roi_net_pct
from app.services.database import close_db, execute, fetch, init_db

CHUNK_SIZE = 10_000


_FETCH_SQL = """
SELECT ds.id,
       ds.outcome,
       ds.current_price,
       m.category
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
 WHERE ds.resolved = true
   AND ds.realized_roi_net IS NULL
 LIMIT $1
"""


_UPDATE_SQL = """
UPDATE detector_signals
   SET realized_roi_net = data.roi_net
  FROM (
    SELECT unnest($1::uuid[])    AS id,
           unnest($2::numeric[]) AS roi_net
  ) AS data
 WHERE detector_signals.id = data.id
"""


async def backfill_chunk(*, dry_run: bool) -> int:
    rows = await fetch(_FETCH_SQL, CHUNK_SIZE)
    if not rows:
        return 0

    ids: list = []
    rois_net: list[float] = []
    for r in rows:
        outcome = r["outcome"] or ""
        if outcome in ("WIN", "LOSS"):
            roi_net = _realized_roi_net_pct(
                outcome,
                float(r["current_price"] or 0.0),
                r["category"],
            )
        else:
            # EXPIRED/VOID/etc — записываем 0.0 явно (не пропуск)
            roi_net = 0.0
        ids.append(r["id"])
        rois_net.append(roi_net)

    if not dry_run:
        await execute(_UPDATE_SQL, ids, rois_net)

    return len(ids)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Не писать в БД")
    args = parser.parse_args()

    await init_db()
    try:
        total = 0
        chunk_idx = 0
        while True:
            chunk_idx += 1
            n = await backfill_chunk(dry_run=args.dry_run)
            total += n
            prefix = "[DRY] " if args.dry_run else ""
            print(f"{prefix}chunk {chunk_idx}: processed={n} cumulative={total}")
            if n < CHUNK_SIZE:
                break
            if args.dry_run:
                # В dry режиме SELECT всегда возвращает CHUNK_SIZE (мы ничего не пишем),
                # потому делаем только один проход для preview.
                print("[DRY] only first chunk shown; rerun without --dry-run to apply.")
                break

        # Финальная проверка
        remaining = await fetch(
            "SELECT COUNT(*) AS n FROM detector_signals "
            "WHERE resolved=true AND realized_roi_net IS NULL"
        )
        print(f"\nDone. total_processed={total} remaining_null={remaining[0]['n']}")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
