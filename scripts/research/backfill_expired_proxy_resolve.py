"""One-off proxy-резолв истории: detector_signals с outcome='EXPIRED', чей market
до сих пор формально НЕ резолвнулся, но цена в резолюционном экстремуме
(markets.current_price ≤0.02 / ≥0.98).

Запускать ТОЛЬКО на проде:
    docker exec oraclepoly-backend python scripts/research/backfill_expired_proxy_resolve.py [--dry-run] [--detector D42_event_driven_ml]

Контекст (фикс №2 аудита virtual-стейджа, research/virtual-stage-audit-2026-07-05/
evidence/07): 97% EXPIRED early-exit строк (746/756) в экстремуме — исход
предрешён, UMA отстаёт, терминальные исходы выпадали из promote-статистики как
roi=0, разбавляя N. detector_signals_resolve теперь proxy-резолвит такие сигналы
forward; этот скрипт лечит уже существующие EXPIRED-строки истории той же
классификацией (_proxy_winner) и теми же ROI-формулами (SSoT).

Комплемент retro_resolve_expired_signals.py: тот чинит EXPIRED с УЖЕ резолвнутым
рынком (по winner), этот — с НЕ резолвнутым, но предрешённым по цене. Прогонять
retro_resolve первым — реальный winner точнее proxy.

Пишет outcome_proxy=true → reconcile-фаза detector_signals_resolve перепроверит
строку по markets.winner, когда UMA догонит (флип исправится автоматически).
execution_orders НЕ трогает напрямую; висящие virtual_filled-позиции этих
сигналов закроет обычный v2_execution_resolve по новому ds.outcome (осознанный
side-effect, см. докстринг detector_signals_resolve).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.jobs.detector_signals_resolve import (
    _proxy_winner,
    _realized_roi_net_pct,
    _realized_roi_pct,
    _signal_outcome,
)
from app.services.database import close_db, execute, fetch, init_db
from app.services.jsonb_utils import parse_jsonb

CHUNK_SIZE = 10_000

_FETCH_SQL = """
SELECT ds.id,
       ds.detector,
       ds.side,
       ds.current_price,
       m.current_price   AS market_price_now,
       m.category        AS category,
       m.fee_schedule    AS fee_schedule
  FROM detector_signals ds
  JOIN markets m ON m.polymarket_id = ds.market_id
 WHERE ds.resolved = true
   AND ds.outcome = 'EXPIRED'
   AND m.resolved = false
   AND (m.current_price <= 0.02 OR m.current_price >= 0.98)
   AND ($1::text IS NULL OR ds.detector = $1)
 LIMIT $2
"""

_UPDATE_SQL = """
UPDATE detector_signals
   SET outcome          = data.outcome,
       realized_roi     = data.realized_roi,
       realized_roi_net = data.realized_roi_net,
       outcome_proxy    = true
  FROM (
    SELECT
      unnest($1::uuid[])    AS id,
      unnest($2::text[])    AS outcome,
      unnest($3::numeric[]) AS realized_roi,
      unnest($4::numeric[]) AS realized_roi_net
  ) AS data
 WHERE detector_signals.id = data.id
"""


async def backfill_chunk(*, detector: str | None, dry_run: bool) -> dict[str, int]:
    rows = await fetch(_FETCH_SQL, detector, CHUNK_SIZE)
    if not rows:
        return {"processed": 0, "wins": 0, "losses": 0, "skipped": 0}

    ids: list = []
    outcomes: list[str] = []
    rois: list[float] = []
    rois_net: list[float] = []
    wins = losses = skipped = 0

    for r in rows:
        winner = _proxy_winner(float(r["market_price_now"]))
        outcome = _signal_outcome(r["side"], winner) if winner else None
        if outcome is None:
            # Цена ушла из экстремума между SELECT и обработкой (defensive)
            skipped += 1
            continue
        current_price = float(r["current_price"])
        rois.append(_realized_roi_pct(outcome, current_price, r["side"]))
        rois_net.append(
            _realized_roi_net_pct(outcome, current_price, r["side"], r["category"], parse_jsonb(r.get("fee_schedule")))
        )
        ids.append(r["id"])
        outcomes.append(outcome)
        if outcome == "WIN":
            wins += 1
        else:
            losses += 1

    if ids and not dry_run:
        await execute(_UPDATE_SQL, ids, outcomes, rois, rois_net)

    return {"processed": len(ids), "wins": wins, "losses": losses, "skipped": skipped}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Не писать в БД, только посчитать")
    parser.add_argument("--detector", default=None, help="Ограничить одним детектором (напр. D42_event_driven_ml)")
    args = parser.parse_args()

    await init_db()
    try:
        total = {"processed": 0, "wins": 0, "losses": 0, "skipped": 0}
        chunk_idx = 0
        while True:
            chunk_idx += 1
            stats = await backfill_chunk(detector=args.detector, dry_run=args.dry_run)
            for k in total:
                total[k] += stats[k]
            prefix = "[DRY] " if args.dry_run else ""
            print(f"{prefix}chunk {chunk_idx}: {stats} cumulative={total}")
            if stats["processed"] + stats["skipped"] < CHUNK_SIZE:
                break
            if args.dry_run:
                print("[DRY] only first chunk shown; rerun without --dry-run to apply.")
                break

        print(f"\nDone. total={total}")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
