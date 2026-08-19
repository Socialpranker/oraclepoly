"""One-off backfill: M2/D45 писали в detector_signals.market_id внутренний UUID
markets.id вместо markets.polymarket_id (0x-condition_id) — resolve-JOIN
(app/jobs/detector_signals_resolve.py: mo.market_id = ds.market_id) никогда не
матчился, 100% сигналов censored (EXPIRED, ноль WIN/LOSS; 3152 M2 + 425 d45 за
60д на момент диагностики 2026-07-09).

Запускать ТОЛЬКО на проде (после деплоя фиксов d45_chainlink_leadlag_scan.py и
m2_vacuum_hook.py, иначе джобы продолжат плодить UUID-строки):
    docker exec oraclepoly-backend python scripts/research/backfill_m2_d45_market_id_uuid_fix.py [--dry-run] [--detector M2_liquidity_vacuum]

Две фазы:
1. Rewrite market_id: UUID → markets.polymarket_id (JOIN m.id::text = ds.market_id).
   Трогает и resolved, и unresolved строки: unresolved после rewrite резолвятся
   штатным cron'ом detector_signals_resolve. Уникальных индексов по market_id у
   M2/d45 нет (dedupe на tx_hash) — коллизии невозможны.
2. Retro-resolve EXPIRED → WIN/LOSS для строк, чей market уже resolved с
   winner IN ('yes','no') — формулы SSoT из app.jobs.detector_signals_resolve
   (по образцу retro_resolve_expired_signals.py). outcome_proxy → false: outcome
   пересчитан по фактическому markets.winner. resolved=false обратно НЕ ставим.
   Строки, чей market ещё не resolved, остаются EXPIRED — скрипт идемпотентен,
   можно перезапускать позже.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.detectors.d45_chainlink_leadlag import DETECTOR_NAME as D45_NAME
from app.detectors.m2_liquidity_vacuum import DETECTOR_NAME as M2_NAME
from app.jobs.detector_signals_resolve import _realized_roi_net_pct, _realized_roi_pct, _signal_outcome
from app.services.database import close_db, execute, fetch, fetchval, init_db
from app.services.jsonb_utils import parse_jsonb

TARGET_DETECTORS = [M2_NAME, D45_NAME]
CHUNK_SIZE = 10_000
SQL_TIMEOUT_S = 600.0  # UPDATE с JOIN по markets — не упираться в пуловый command_timeout

_COUNT_UUID_ROWS_SQL = """
SELECT count(*)
  FROM detector_signals ds
  JOIN markets m ON m.id::text = ds.market_id
 WHERE ds.detector = ANY($1::text[])
   AND m.polymarket_id IS NOT NULL
"""

_REWRITE_MARKET_ID_SQL = """
UPDATE detector_signals ds
   SET market_id = m.polymarket_id
  FROM markets m
 WHERE m.id::text = ds.market_id
   AND m.polymarket_id IS NOT NULL
   AND ds.detector = ANY($1::text[])
"""

_FETCH_EXPIRED_SQL = """
SELECT ds.id,
       ds.side,
       ds.current_price,
       m.winner       AS market_winner,
       m.category     AS category,
       m.fee_schedule AS fee_schedule
  -- OR-джойн: работает и до фазы 1 (market_id = UUID), и после (= polymarket_id) —
  -- иначе dry-run (фаза 1 ничего не переписала) показывал бы 0 кандидатов фазы 2.
  -- Строк у M2/d45 тысячи, seq-scan по detector-фильтру приемлем для one-off.
  FROM detector_signals ds
  JOIN markets m ON (m.polymarket_id = ds.market_id OR m.id::text = ds.market_id)
 WHERE ds.detector = ANY($1::text[])
   AND ds.resolved = true
   AND ds.outcome = 'EXPIRED'
   AND m.resolved = true
   AND m.winner IN ('yes', 'no')
 LIMIT $2
"""

_UPDATE_OUTCOME_SQL = """
UPDATE detector_signals
   SET outcome          = data.outcome,
       realized_roi     = data.realized_roi,
       realized_roi_net = data.realized_roi_net,
       outcome_proxy    = false
  FROM (
    SELECT
      unnest($1::uuid[])    AS id,
      unnest($2::text[])    AS outcome,
      unnest($3::numeric[]) AS realized_roi,
      unnest($4::numeric[]) AS realized_roi_net
  ) AS data
 WHERE detector_signals.id = data.id
"""


async def rewrite_market_ids(detectors: list[str], dry_run: bool) -> int:
    """Фаза 1: UUID → polymarket_id. Возвращает число затронутых строк."""
    candidates = await fetchval(_COUNT_UUID_ROWS_SQL, detectors)
    if dry_run:
        print(f"[DRY] phase 1: {candidates} строк с UUID market_id → polymarket_id")
        return int(candidates or 0)
    status = await execute(_REWRITE_MARKET_ID_SQL, detectors, timeout=SQL_TIMEOUT_S)
    updated = int(status.split()[-1])
    print(f"phase 1: rewritten {updated} строк (кандидатов было {candidates})")
    return updated


async def retro_resolve_chunk(detectors: list[str], dry_run: bool) -> dict[str, int]:
    """Фаза 2: один chunk EXPIRED → WIN/LOSS (формулы = detector_signals_resolve)."""
    rows = await fetch(_FETCH_EXPIRED_SQL, detectors, CHUNK_SIZE)
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
            # winner неожиданно не yes/no несмотря на SQL-фильтр (defensive)
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
        await execute(_UPDATE_OUTCOME_SQL, ids, outcomes, rois, rois_net, timeout=SQL_TIMEOUT_S)

    return {"processed": len(ids), "wins": wins, "losses": losses, "skipped_void": skipped_void}


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
        await rewrite_market_ids(detectors, dry_run=args.dry_run)

        total = {"processed": 0, "wins": 0, "losses": 0, "skipped_void": 0}
        chunk_idx = 0
        while True:
            chunk_idx += 1
            stats = await retro_resolve_chunk(detectors, dry_run=args.dry_run)
            for k in total:
                total[k] += stats[k]
            prefix = "[DRY] " if args.dry_run else ""
            print(f"{prefix}phase 2 chunk {chunk_idx}: {stats} cumulative={total}")
            if stats["processed"] + stats["skipped_void"] < CHUNK_SIZE:
                break
            if args.dry_run:
                # dry-run не апдейтит строки → повторный fetch вернул бы тот же chunk
                print("[DRY] показан только первый chunk; без --dry-run обработаются все.")
                break

        print(f"\nDone. phase 2 total={total}")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
