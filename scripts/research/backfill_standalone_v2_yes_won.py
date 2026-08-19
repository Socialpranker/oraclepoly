"""One-off backfill: standalone_v2_predictions.{yes_won, resolved_at} из markets.winner.

Запускать ТОЛЬКО на проде:
    docker exec oraclepoly-backend python scripts/research/backfill_standalone_v2_yes_won.py [--dry-run]

Контекст 2026-05-19 (post-pipeline audit):
    standalone_v2_predictions имеет 14,812 строк, но yes_won=NULL у ВСЕХ —
    автоматический resolve-cron для этой таблицы никогда не настраивался.
    4,008 из них имеют связанный resolved market (markets.resolved=true и
    winner IN ('yes','no')) — заполняем yes_won deterministically.

Используется для L2 retrain dataset (combined с pipeline_v2_shadow_predictions),
чтобы получить ~7,181 labeled samples вместо текущих 3,173.

Идемпотентно: WHERE sv.yes_won IS NULL — повторный прогон ничего не делает.

Sanity:
  - До: yes_won IS NULL для всех → COUNT 14,812.
  - Из них matchable (markets.resolved=true, winner IN yes/no) → ~4,008.
  - После: ~4,008 строк с yes_won IS NOT NULL, остальные ещё ждут резолва markets.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.database import close_db, execute, fetch, fetchrow, init_db  # noqa: E402

_COUNT_NULL_SQL = """
SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE sv.yes_won IS NULL) AS unlabeled,
    COUNT(*) FILTER (
        WHERE sv.yes_won IS NULL
          AND m.resolved = true
          AND m.winner IN ('yes', 'no')
    ) AS resolvable_now
FROM standalone_v2_predictions sv
LEFT JOIN markets m ON m.polymarket_id = sv.market_id
"""

_PREVIEW_SQL = """
SELECT sv.market_id,
       sv.anchor_hours,
       sv.predicted_p_yes,
       m.winner,
       (CASE WHEN m.winner = 'yes' THEN 1 ELSE 0 END) AS new_yes_won
FROM standalone_v2_predictions sv
JOIN markets m ON m.polymarket_id = sv.market_id
WHERE sv.yes_won IS NULL
  AND m.resolved = true
  AND m.winner IN ('yes', 'no')
LIMIT 5
"""

_UPDATE_SQL = """
UPDATE standalone_v2_predictions sv
   SET yes_won = CASE WHEN m.winner = 'yes' THEN 1 ELSE 0 END,
       resolved_at = COALESCE(sv.resolved_at, m.updated_at, now())
  FROM markets m
 WHERE m.polymarket_id = sv.market_id
   AND m.resolved = true
   AND m.winner IN ('yes', 'no')
   AND sv.yes_won IS NULL
"""

_POST_BACKFILL_DISTRIB_SQL = """
SELECT
    COUNT(*) FILTER (WHERE yes_won = 1) AS wins,
    COUNT(*) FILTER (WHERE yes_won = 0) AS losses,
    COUNT(*) FILTER (WHERE yes_won IS NOT NULL) AS labeled_total,
    ROUND(AVG(predicted_p_yes) FILTER (WHERE yes_won IS NOT NULL)::numeric, 4) AS avg_predicted,
    ROUND(AVG(yes_won::float) FILTER (WHERE yes_won IS NOT NULL)::numeric, 4) AS actual_wr
FROM standalone_v2_predictions
"""


async def main(dry_run: bool) -> None:
    await init_db()
    try:
        pre = await fetchrow(_COUNT_NULL_SQL)
        print("─── Pre-backfill ───────────────────────────────────────────────")
        print(f"  total rows:        {pre['total']:>6}")
        print(f"  unlabeled (NULL):  {pre['unlabeled']:>6}")
        print(f"  resolvable now:    {pre['resolvable_now']:>6}")

        if pre["resolvable_now"] == 0:
            print("\n✓ Nothing to backfill — idempotent. Exiting.")
            return

        print("\n─── Preview (first 5 rows to update) ──────────────────────────")
        preview = await fetch(_PREVIEW_SQL)
        for r in preview:
            print(
                f"  market={r['market_id'][:16]}.. "
                f"anchor={r['anchor_hours']:>3}h "
                f"pred={float(r['predicted_p_yes']):.3f} "
                f"winner={r['winner']:>3} "
                f"→ yes_won={r['new_yes_won']}"
            )

        if dry_run:
            print("\n[DRY RUN] No changes applied.")
            return

        print("\n─── Applying UPDATE ───────────────────────────────────────────")
        await execute(_UPDATE_SQL)
        print("  UPDATE executed.")

        post = await fetchrow(_POST_BACKFILL_DISTRIB_SQL)
        print("\n─── Post-backfill distribution ────────────────────────────────")
        print(f"  labeled total:     {post['labeled_total']:>6}")
        print(f"  wins (yes_won=1):  {post['wins']:>6}")
        print(f"  losses (yes_won=0):{post['losses']:>6}")
        print(f"  avg predicted_p:   {float(post['avg_predicted']):.4f}")
        print(f"  actual WR:         {float(post['actual_wr']):.4f}")

        verify = await fetchrow(_COUNT_NULL_SQL)
        remaining = verify["resolvable_now"]
        if remaining > 0:
            print(f"\n⚠ {remaining} rows ещё помечены как resolvable но НЕ обновились — investigate.")
        else:
            print("\n✓ Все resolvable rows обновлены.")
    finally:
        await close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview without UPDATE")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
