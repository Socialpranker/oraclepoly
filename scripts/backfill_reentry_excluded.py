"""Backfill: пометить исторические re-entry позиции meta.reentry_excluded=true.

Контекст (standalone_virtual_pnl_inflation_2026-06-04): STANDALONE_V2_LGB до
event-guard (PR #560) заходил в один neg-risk event 10-32 раза за 16ч (TP=+30%
за ~12 мин → settled → новый сигнал → вход снова). Эти re-entry позиции раздули:
  - промоут-гейт (detector_metrics): видел +$41K/391 settled vs честный +$781/28
  - дашборд daily_summary: ~+$48K фантома за 14д vs flat

Event-guard теперь блокирует re-entry forward в ОБОИХ режимах (virtual+live, fix
2026-06-04). Но 432 исторические re-entry позиции остаются в БД. Этот скрипт
помечает их meta.reentry_excluded=true — агрегаты (гейт/дашборд/TG) фильтруют
помеченные (AND COALESCE((meta->>'reentry_excluded')::bool,false)=false).

Единица дедупа: ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY created_at) > 1.
Первый вход на event (rn=1) — честный, НЕ помечается. Повторы (rn>1) — помечаются.

ОБРАТИМО: данные НЕ удаляются, только добавляется ключ в meta jsonb. Откат:
  UPDATE execution_orders SET meta = meta - 'reentry_excluded'
  WHERE (meta->>'reentry_excluded')::bool = true;

Idempotent: повторный запуск не меняет результат (уже помеченные пропускаются по
тому же rn>1 предикату; meta || перезаписывает тем же значением).

Скоуп: все virtual_settled/filled/placed позиции (НЕ rejected/cancelled). По
умолчанию все детекторы (универсально), но на практике только STANDALONE имеет
re-entry (остальные max 1 поз/event — verified). Можно сузить через --detector.

Usage (ТОЛЬКО на проде, БД пустая локально):
    python scripts/backfill_reentry_excluded.py --dry-run
    python scripts/backfill_reentry_excluded.py --apply
    python scripts/backfill_reentry_excluded.py --apply --detector STANDALONE_V2_LGB
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


# Помечаем re-entry: rn>1 в окне (detector, event_id) по времени создания.
# Считаем только реальные позиции (не rejected/cancelled) — несостоявшийся вход
# не «занимает» event. event_id IS NOT NULL — без события дедуп не имеет смысла.
_FETCH_REENTRY_SQL = """
WITH ranked AS (
    SELECT
        id, detector, event_id, created_at,
        COALESCE(net_pnl, pnl)::float AS net_pnl,
        (meta->>'reentry_excluded')::bool AS already_marked,
        ROW_NUMBER() OVER (PARTITION BY detector, event_id ORDER BY created_at) AS rn
    FROM execution_orders
    WHERE execution_mode = 'virtual'
      AND status IN ('virtual_placed', 'virtual_filled', 'virtual_settled')
      AND event_id IS NOT NULL
      AND ($1::text IS NULL OR detector = $1)
)
SELECT id, detector, event_id, net_pnl, COALESCE(already_marked, false) AS already_marked
FROM ranked
WHERE rn > 1
ORDER BY detector, event_id, created_at
"""

# Phantom-TP исторические сделки (2026-06-05, см. standalone_virtual_pnl_inflation
# §6.3 + PR #568). Forward-guard _is_phantom_tp_exit блокирует НОВЫЕ, но историч.
# сделки остались. Критерий фантома (тот же что в position_manager guard):
# take_profit + hold ≤20мин + exit на резолюционном экстремуме (≤0.02 OR ≥0.98).
# Помечаем ТЕМ ЖЕ meta.reentry_excluded — фильтр уже во всех 11 агрегатах
# (дашборд+гейт), 0 нового кода. Большинство STANDALONE phantom-TP уже помечены
# как re-entry (overlap 159/165); этот режим добивает остаток (STANDALONE 6, D3 3).
# D3 short-hold extreme = тоже фантом (НЕ long-hold арбитраж — тот >20мин, не ловится).
_FETCH_PHANTOM_TP_SQL = """
SELECT
    id, detector,
    COALESCE(net_pnl, pnl)::float AS net_pnl,
    COALESCE((meta->>'reentry_excluded')::bool, false) AS already_marked
FROM execution_orders
WHERE execution_mode = 'virtual'
  AND status = 'virtual_settled'
  AND exit_reason = 'take_profit'
  AND filled_at IS NOT NULL AND settled_at IS NOT NULL
  AND EXTRACT(EPOCH FROM (settled_at - filled_at)) / 60.0 <= 20
  AND (exit_price <= 0.02 OR exit_price >= 0.98)
  AND ($1::text IS NULL OR detector = $1)
ORDER BY detector, created_at
"""

_UPDATE_SQL = """
UPDATE execution_orders
   SET meta = COALESCE(meta, '{}'::jsonb) || '{"reentry_excluded": true}'::jsonb
 WHERE id = $1
"""


def _summarize(rows: list, label: str) -> list:
    """Печать сводки по детекторам + возврат списка к пометке (не already_marked)."""
    to_mark = [r for r in rows if not r["already_marked"]]
    already = sum(1 for r in rows if r["already_marked"])
    per_detector: dict[str, dict[str, float]] = {}
    for r in rows:
        d = per_detector.setdefault(r["detector"], {"n": 0, "phantom_pnl": 0.0})
        d["n"] += 1
        d["phantom_pnl"] += r["net_pnl"] or 0.0
    print(f"\n[{label}] найдено: {len(rows)} (уже помечены: {already}, к пометке: {len(to_mark)})")
    print(f"{'detector':<32} {'n':>6} {'phantom_pnl':>14}")
    print("-" * 54)
    for det, d in sorted(per_detector.items(), key=lambda x: -x[1]["n"]):
        print(f"{det:<32} {d['n']:>6} {d['phantom_pnl']:>14.2f}")
    total = sum(d["phantom_pnl"] for d in per_detector.values())
    print("-" * 54)
    print(f"{'TOTAL':<32} {len(rows):>6} {total:>14.2f}")
    return to_mark


async def main(apply: bool, detector: str | None, include_phantom_tp: bool) -> None:
    import asyncpg

    from app.core.config import get_settings

    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        reentry_rows = await conn.fetch(_FETCH_REENTRY_SQL, detector)
        to_mark = _summarize(reentry_rows, "RE-ENTRY (rn>1)") if reentry_rows else []

        # Phantom-TP режим: добавляем фантомные TP-сделки (§6.3) к тому же набору.
        # Дедуп по id: phantom-TP, уже попавший в re-entry, не помечаем дважды.
        if include_phantom_tp:
            phantom_rows = await conn.fetch(_FETCH_PHANTOM_TP_SQL, detector)
            reentry_ids = {r["id"] for r in reentry_rows}
            phantom_to_mark = _summarize(
                [r for r in phantom_rows if r["id"] not in reentry_ids], "PHANTOM-TP (extreme+hold≤20м)"
            )
            seen = {r["id"] for r in to_mark}
            to_mark = to_mark + [r for r in phantom_to_mark if r["id"] not in seen]

        if not to_mark:
            print("\nНечего помечать.")
            return

        if not apply:
            print(f"\nDRY-RUN: к пометке {len(to_mark)} позиций. Запусти с --apply.")
            return

        marked = 0
        for r in to_mark:
            await conn.execute(_UPDATE_SQL, r["id"])
            marked += 1
        print(f"\nAPPLIED: помечено {marked} позиций meta.reentry_excluded=true.")
        print("Откат: UPDATE execution_orders SET meta = meta - 'reentry_excluded' "
              "WHERE (meta->>'reentry_excluded')::bool = true;")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mark historical re-entry + phantom-TP positions")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="preview, ничего не менять")
    g.add_argument("--apply", action="store_true", help="применить пометки")
    parser.add_argument("--detector", default=None, help="ограничить одним детектором")
    parser.add_argument(
        "--include-phantom-tp",
        action="store_true",
        help="также пометить фантом-TP сделки (§6.3): take_profit + hold≤20мин + extreme exit",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply, detector=args.detector, include_phantom_tp=args.include_phantom_tp))
