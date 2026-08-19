"""One-off retro-резолв: detector_signals с outcome='EXPIRED', чей market уже
фактически резолвнулся (markets.resolved=true AND winner IN ('yes','no')).

Запускать ТОЛЬКО на проде:
    docker exec oraclepoly-backend python scripts/research/retro_resolve_expired_signals.py [--dry-run] [--detector D3_cross_mkt_arb]

Контекст (research/fable-gates-audit-2026-07-03/, evidence/06 §A/§D):
EXPIRE_HORIZON_MULTIPLIER=6 давал EXPIRE для D3_cross_mkt_arb через 36ч
(HORIZON_SEC=6ч), а d3_scan сканирует события с end_date до 30 дней вперёд —
1328/1678 (79%) D3-сигналов истории помечены EXPIRED с realized_roi=0, хотя
market к моменту аудита уже резолвнулся. detector_signals_resolve.py:44-52
теперь даёт D3 множитель 120 (=30д) для будущих сигналов — этот скрипт лечит
уже существующие EXPIRED-строки истории тем же WIN/LOSS-расчётом.

Trade-off явно: НЕ ставит resolved=false обратно (сигнал уже resolved), только
перезаписывает outcome/realized_roi/realized_roi_net для строк, где
market_outcome теперь известен. execution_orders НЕ трогает — это отдельная
таблица, ретро-резолв реальных денег закрывается отдельным settle-фиксом.

Формулы идентичны app.jobs.detector_signals_resolve (SSoT), детектор явно
не ограничен — по умолчанию обрабатывает все детекторы, --detector сужает
до одного (полезно для точечного прогона/dry-run на D3 без риска задеть
остальные детекторы одним batch'ем).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.jobs.detector_signals_resolve import _realized_roi_net_pct, _realized_roi_pct, _signal_outcome
from app.services.database import close_db, execute, fetch, init_db
from app.services.jsonb_utils import parse_jsonb

CHUNK_SIZE = 10_000

_FETCH_SQL = """
SELECT ds.id,
       ds.detector,
       ds.side,
       ds.current_price,
       m.winner         AS market_winner,
       m.category        AS category,
       m.fee_schedule    AS fee_schedule
  FROM detector_signals ds
  JOIN markets m ON m.polymarket_id = ds.market_id
 WHERE ds.resolved = true
   AND ds.outcome = 'EXPIRED'
   AND m.resolved = true
   AND m.winner IN ('yes', 'no')
   AND ($1::text IS NULL OR ds.detector = $1)
 LIMIT $2
"""

_UPDATE_SQL = """
UPDATE detector_signals
   SET outcome          = data.outcome,
       realized_roi     = data.realized_roi,
       realized_roi_net = data.realized_roi_net
  FROM (
    SELECT
      unnest($1::uuid[])    AS id,
      unnest($2::text[])    AS outcome,
      unnest($3::numeric[]) AS realized_roi,
      unnest($4::numeric[]) AS realized_roi_net
  ) AS data
 WHERE detector_signals.id = data.id
"""


async def retro_resolve_chunk(*, detector: str | None, dry_run: bool) -> dict[str, int]:
    rows = await fetch(_FETCH_SQL, detector, CHUNK_SIZE)
    if not rows:
        return {"processed": 0, "wins": 0, "losses": 0, "skipped_void": 0}

    ids: list = []
    outcomes: list[str] = []
    rois: list[float] = []
    rois_net: list[float] = []
    wins = losses = skipped_void = 0

    for r in rows:
        outcome = _signal_outcome(r["side"], r["market_winner"])
        if outcome is None:
            # winner неожиданно не yes/no несмотря на SQL-фильтр (defensive) — пропускаем
            skipped_void += 1
            continue
        current_price = float(r["current_price"])
        roi = _realized_roi_pct(outcome, current_price, r["side"])
        roi_net = _realized_roi_net_pct(
            outcome, current_price, r["side"], r["category"], parse_jsonb(r.get("fee_schedule"))
        )
        ids.append(r["id"])
        outcomes.append(outcome)
        rois.append(roi)
        rois_net.append(roi_net)
        if outcome == "WIN":
            wins += 1
        else:
            losses += 1

    if ids and not dry_run:
        await execute(_UPDATE_SQL, ids, outcomes, rois, rois_net)

    return {"processed": len(ids), "wins": wins, "losses": losses, "skipped_void": skipped_void}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Не писать в БД, только посчитать")
    parser.add_argument("--detector", default=None, help="Ограничить одним детектором (напр. D3_cross_mkt_arb)")
    args = parser.parse_args()

    await init_db()
    try:
        total = {"processed": 0, "wins": 0, "losses": 0, "skipped_void": 0}
        chunk_idx = 0
        while True:
            chunk_idx += 1
            stats = await retro_resolve_chunk(detector=args.detector, dry_run=args.dry_run)
            for k in total:
                total[k] += stats[k]
            prefix = "[DRY] " if args.dry_run else ""
            print(f"{prefix}chunk {chunk_idx}: {stats} cumulative={total}")
            if stats["processed"] + stats["skipped_void"] < CHUNK_SIZE:
                break
            if args.dry_run:
                print("[DRY] only first chunk shown; rerun without --dry-run to apply.")
                break

        print(f"\nDone. total={total}")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
