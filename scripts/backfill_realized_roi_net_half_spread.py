"""Backfill: пересчитать realized_roi_net с учётом half-spread.

Баг (исправлен 2026-08-05 в app/jobs/detector_signals_resolve.py): net-ROI
считался по цене входа = МИД (`detector_signals.current_price` — это
`(best_bid+best_ask)/2` из WS), хотя покупаем по ask. Отчётность была системно
оптимистичнее сайзера, который half-spread уже вычитал (`ev_sizer._cost_drag_pct`).

Эффект — сдвиг ≈2.5 п.п., и он МЕНЯЕТ ВЕРДИКТ по детекторам:
    D38_resolution_lag  +0.27% → −2.15%   (был единственный «прибыльный»)
    D26_fade_whale_extreme +0.96% → −1.25%
Остальные просто честно хуже. `realized_roi_net` — вход промоут-гейта
(`promote_gates.py`), то есть решения о включении детекторов принимались по
завышенной планке.

Почему скрипт, а не SQL-миграция: 80% строк (241k из 303k) имеют per-market
`markets.fee_schedule` (JSONB), который учитывается только Python-функцией
`estimate_trading_fee`. Чистый SQL проигнорировал бы его и разошёлся бы с
forward-расчётом джобы — то есть создал бы новую неконсистентность вместо
починки. Здесь переиспользуется ТА ЖЕ функция `_realized_roi_net_pct`, что
считает новые резолвы, поэтому расхождение невозможно по конструкции.

Gross `realized_roi` НЕ трогаем: на нём учатся ML L1/L2 binary, смена
определения потребовала бы синхронного re-fit. Gross по смыслу «без издержек».

Idempotent: формула детерминирована, повторный прогон даёт тот же результат.

Usage:
    python scripts/backfill_realized_roi_net_half_spread.py --dry-run
    python scripts/backfill_realized_roi_net_half_spread.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

BATCH_SIZE = 5000

# resolved_at IS NOT NULL — берём только уже посчитанные строки: у неразрешённых
# realized_roi_net и так NULL, трогать нечего.
_FETCH_SQL = """
SELECT
    ds.id,
    ds.detector,
    ds.side,
    ds.outcome,
    ds.current_price::float          AS current_price,
    ds.realized_roi_net::float       AS old_roi_net,
    m.category,
    m.fee_schedule
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
 WHERE ds.realized_roi_net IS NOT NULL
   AND ds.current_price IS NOT NULL
 ORDER BY ds.id
 LIMIT $1 OFFSET $2
"""

_UPDATE_SQL = """
UPDATE detector_signals AS ds
   SET realized_roi_net = v.roi_net
  FROM (SELECT unnest($1::uuid[]) AS id, unnest($2::numeric[]) AS roi_net) AS v
 WHERE ds.id = v.id
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="посчитать и показать, ничего не писать")
    group.add_argument("--apply", action="store_true", help="записать в БД")
    args = parser.parse_args()

    from app.jobs.detector_signals_resolve import _realized_roi_net_pct
    from app.services.database import close_db, execute, fetch, init_db
    from app.services.jsonb_utils import parse_jsonb

    await init_db()
    try:
        offset = 0
        n_seen = 0
        n_changed = 0
        sum_old = 0.0
        sum_new = 0.0
        per_detector: dict[str, dict[str, float]] = {}

        while True:
            rows = await fetch(_FETCH_SQL, BATCH_SIZE, offset)
            if not rows:
                break

            ids: list = []
            vals: list[float] = []
            for r in rows:
                n_seen += 1
                new_roi = _realized_roi_net_pct(
                    r["outcome"],
                    float(r["current_price"]),
                    r["side"],
                    r["category"],
                    parse_jsonb(r["fee_schedule"]),
                )
                old_roi = float(r["old_roi_net"])
                sum_old += old_roi
                sum_new += new_roi

                d = per_detector.setdefault(r["detector"], {"n": 0, "old": 0.0, "new": 0.0})
                d["n"] += 1
                d["old"] += old_roi
                d["new"] += new_roi

                # Сравнение с допуском: БД хранит numeric(8,3), а float-арифметика
                # даёт хвосты — без допуска «изменённых» окажутся все строки.
                if abs(new_roi - old_roi) > 0.0005:
                    n_changed += 1
                    ids.append(r["id"])
                    vals.append(new_roi)

            if args.apply and ids:
                await execute(_UPDATE_SQL, ids, vals)

            offset += BATCH_SIZE
            print(f"  …обработано {n_seen}, изменилось {n_changed}", flush=True)

        mode = "ПРИМЕНЕНО" if args.apply else "DRY-RUN (ничего не записано)"
        print(f"\n=== {mode} ===")
        print(f"строк просмотрено : {n_seen}")
        print(f"строк изменилось  : {n_changed}")
        if n_seen:
            print(f"средний ROI до    : {sum_old / n_seen:+.3f}%")
            print(f"средний ROI после : {sum_new / n_seen:+.3f}%")
            print(f"сдвиг             : {(sum_new - sum_old) / n_seen:+.3f} п.п.")

        print("\nПо детекторам (n > 50), отсортировано по сдвигу:")
        print(f"  {'детектор':<32} {'n':>7} {'было':>9} {'стало':>9} {'сдвиг':>8}")
        rows_out = [
            (name, d["n"], d["old"] / d["n"], d["new"] / d["n"]) for name, d in per_detector.items() if d["n"] > 50
        ]
        for name, n, old_avg, new_avg in sorted(rows_out, key=lambda x: x[3] - x[2]):
            flip = "  ← ЗНАК СМЕНИЛСЯ" if (old_avg > 0) != (new_avg > 0) else ""
            print(f"  {name:<32} {n:>7} {old_avg:>+9.2f} {new_avg:>+9.2f} {new_avg - old_avg:>+8.2f}{flip}")

        return 0
    finally:
        await close_db()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
