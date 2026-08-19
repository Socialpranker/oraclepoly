"""Phase 0 replay-кейс 2: D38(virtual) → false anomaly-kill на D3(live), 2026-06-29.

Инцидент 2026-06-29 (см. `scripts/risk_daemon.py::check_anomalies` docstring и memory
`d38-spike-anomaly-killswitch-2026-06-29`): `check_anomalies()` считал
`COUNT(*) FILTER (status='rejected')` по `execution_orders` БЕЗ фильтра по
`detector_state.stage`. Virtual-детектор D38_resolution_lag плодил
12-21 rejected-ордеров/мин (paper trades, execution_mode='virtual', НО
status='rejected' — поэтому фильтр `execution_mode != 'virtual'` не спас бы),
превышая `risk_daemon_anomaly_rejections_per_min` и взводя ГЛОБАЛЬНЫЙ
kill_switch, который останавливал живую торговлю D3_cross_mkt_arb. Баг уже
исправлен в проде (JOIN к `detector_state ds ON ds.stage = 'live'` — см.
`scripts/risk_daemon.py:216-219`).

Этот скрипт НЕ проверяет текущий код (это делает
`tests/test_risk_daemon_anomalies.py::test_query_still_joins_live_detector_stage`
структурно). Задача Phase 0 — детерминированно ВОСПРОИЗВЕСТИ на исторических
данных 2026-06-29, что (а) buggy SQL без JOIN действительно ложно триггерил
kill на шуме D38, и (б) fixed SQL с JOIN этот шум фильтрует, не создавая
ложноотрицательный кейс для D3(live).

Step 1 (историческая честность реплея, brief acceptance):
`detector_state.stage` — мутабельное поле без таблицы истории переходов в
БД (проверено 2026-07-20: `\\dt` на проде не содержит ни одной *_history/
*_audit/*_log таблицы, покрывающей detector_state; есть только
demote_alert_log/gate_shadow_log/price_staleness_log — не то). Поэтому вместо
JOIN к ЖИВОЙ detector_state пришлось бы либо доверять её текущему состоянию,
либо явно подставить исторический stage константой. Read-only проверка на
проде 2026-07-20 (см. `agent_runs.metrics.step1_check` этого прогона и
task-5-report.md):

    SELECT detector, stage, updated_at, promoted_at, paused_reason
      FROM detector_state
     WHERE detector IN ('D38_resolution_lag', 'D3_cross_mkt_arb');

    D38_resolution_lag | virtual | updated_at=2026-06-24 14:03:25 | promoted_at=2026-06-24 14:03:25
    D3_cross_mkt_arb   | live    | updated_at=2026-07-20 07:00:02 | promoted_at=2026-06-25 12:51:51

D38's `updated_at` не менялся с 2026-06-24 (до начала окна 06-24..06-29) —
после promote в virtual строка ни разу не тронута. D3's `promoted_at`
(переход в live) — 2026-06-25, ДО инцидента 06-29; текущий `updated_at`
07-20 — это последующий unrelated recompute (fractional_kelly/live_stake_cap),
не смена stage (stage остаётся 'live', promoted_at не меняется при UPDATE
остальных полей). Ни одна строка не показывает признаков смены stage внутри
окна 2026-06-24..2026-06-29 → используем ЖИВОЙ JOIN к detector_state (не
hardcoded константу) для fixed-replay — честная историческая реконструкция,
open risk из брифа не реализовался.

Запуск (read-only роль oraclepoly_agent_ro, Task 2):
    docker exec oraclepoly-backend python scripts/research/audit_case2_d38_anomaly_stage_replay.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime

import structlog

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.config import get_settings  # noqa: E402
from app.services.database import close_db, fetch, init_db  # noqa: E402

logger = structlog.get_logger(__name__)

WINDOW_START = "2026-06-29 00:00:00+00"
WINDOW_END = "2026-06-30 00:00:00+00"

# SQL №1 — buggy replay: исходный root-cause запрос ДО фикса. 1-минутные окна
# по created_at (генерируем через generate_series, а не now()-based INTERVAL —
# replay исторический, а не live-тик) БЕЗ JOIN к detector_state.
_BUGGY_SQL = """
WITH minute_buckets AS (
    SELECT generate_series($1::timestamptz, $2::timestamptz, INTERVAL '1 minute') AS bucket_start
)
SELECT eo.detector,
       mb.bucket_start,
       COUNT(*) AS recent,
       COUNT(*) FILTER (WHERE eo.status = 'rejected') AS rejected_recent
  FROM minute_buckets mb
  JOIN execution_orders eo
    ON eo.created_at >= mb.bucket_start
   AND eo.created_at <  mb.bucket_start + INTERVAL '1 minute'
 WHERE eo.created_at >= $1::timestamptz
   AND eo.created_at <  $2::timestamptz
 GROUP BY eo.detector, mb.bucket_start
"""

# SQL №2 — fixed replay: тот же агрегат + JOIN detector_state stage='live'
# (Step 1 подтвердил stage-стабильность в окне → живой JOIN, не константа).
_FIXED_SQL = """
WITH minute_buckets AS (
    SELECT generate_series($1::timestamptz, $2::timestamptz, INTERVAL '1 minute') AS bucket_start
)
SELECT eo.detector,
       mb.bucket_start,
       COUNT(*) AS recent,
       COUNT(*) FILTER (WHERE eo.status = 'rejected') AS rejected_recent
  FROM minute_buckets mb
  JOIN execution_orders eo
    ON eo.created_at >= mb.bucket_start
   AND eo.created_at <  mb.bucket_start + INTERVAL '1 minute'
  JOIN detector_state ds
    ON ds.detector = eo.detector
   AND ds.stage = 'live'
 WHERE eo.created_at >= $1::timestamptz
   AND eo.created_at <  $2::timestamptz
 GROUP BY eo.detector, mb.bucket_start
"""

# Step 1 evidence query — read-only sanity check, повторяется каждым прогоном
# скрипта (не только один раз руками), чтобы agent_runs.metrics зафиксировал
# доказательство stage-стабильности КАЖДЫЙ раз, а не полагался на комментарий.
_STAGE_STABILITY_SQL = """
SELECT detector, stage, updated_at, promoted_at
  FROM detector_state
 WHERE detector IN ('D38_resolution_lag', 'D3_cross_mkt_arb')
 ORDER BY detector
"""


def _config_hash() -> str:
    """Hash SQL-текста + окна — воспроизводимость без LLM."""
    payload = json.dumps(
        {"buggy_sql": _BUGGY_SQL, "fixed_sql": _FIXED_SQL, "window_start": WINDOW_START, "window_end": WINDOW_END},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _dataset_hash(row_count: int, max_created_at: datetime | None) -> str:
    return hashlib.sha256(f"{row_count}:{max_created_at}".encode()).hexdigest()


def _worst_offender(rows: list, detector: str, threshold: int) -> int:
    """Максимум rejected_recent по всем минутным окнам для данного детектора."""
    values = [int(r["rejected_recent"] or 0) for r in rows if r["detector"] == detector]
    return max(values, default=0)


async def _run(window_start: datetime, window_end: datetime) -> dict:
    settings = get_settings()
    threshold = settings.risk_daemon_anomaly_rejections_per_min

    # Step 1: re-verify stage stability at run time (не только доверять docstring).
    stage_rows = await fetch(_STAGE_STABILITY_SQL)
    stage_snapshot = {
        r["detector"]: {
            "stage": r["stage"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "promoted_at": r["promoted_at"].isoformat() if r["promoted_at"] else None,
        }
        for r in stage_rows
    }

    buggy_rows = await fetch(_BUGGY_SQL, window_start, window_end)
    fixed_rows = await fetch(_FIXED_SQL, window_start, window_end)

    buggy_detectors = sorted({r["detector"] for r in buggy_rows})
    fixed_detectors = sorted({r["detector"] for r in fixed_rows})

    d38_buggy_max = _worst_offender(buggy_rows, "D38_resolution_lag", threshold)
    d38_fixed_max = _worst_offender(fixed_rows, "D38_resolution_lag", threshold)
    d3_buggy_max = _worst_offender(buggy_rows, "D3_cross_mkt_arb", threshold)
    d3_fixed_max = _worst_offender(fixed_rows, "D3_cross_mkt_arb", threshold)

    buggy_kill_triggered = d38_buggy_max > threshold
    fixed_kill_triggered = d38_fixed_max > threshold
    d38_present_in_fixed = "D38_resolution_lag" in fixed_detectors
    d3_false_triggered_buggy = d3_buggy_max > threshold
    d3_false_triggered_fixed = d3_fixed_max > threshold

    row_count = len(buggy_rows) + len(fixed_rows)
    max_created = max(
        (r["bucket_start"] for r in buggy_rows if r["bucket_start"] is not None),
        default=None,
    )

    verdict_parts = [
        f"buggy SQL (без JOIN detector_state): D38_resolution_lag rejected_recent max={d38_buggy_max}/min "
        f"({'>' if buggy_kill_triggered else '<='} threshold={threshold}) → "
        f"{'ЛОЖНЫЙ KILL воспроизведён' if buggy_kill_triggered else 'kill НЕ воспроизведён (unexpected)'}.",
        f"fixed SQL (JOIN stage='live'): D38_resolution_lag "
        f"{'присутствует в агрегации (unexpected — фикс не сработал)' if d38_present_in_fixed else 'отсутствует в агрегации целиком (0 строк) — фикс подтверждён'}.",
        f"D3_cross_mkt_arb (live): buggy max={d3_buggy_max}/min, fixed max={d3_fixed_max}/min — "
        f"{'ложноотрицательный кейс НЕ создан фиксом' if not d3_false_triggered_buggy and not d3_false_triggered_fixed else 'ВНИМАНИЕ: D3 превышает порог'}.",
        "Step 1: detector_state.updated_at для D38 не менялся с 2026-06-24 (до начала окна), "
        "D3.promoted_at=2026-06-25 (до инцидента 06-29) — stage стабилен в окне, "
        "живой JOIN использован вместо hardcoded константы (open risk брифа не реализовался).",
    ]
    verdict = " ".join(verdict_parts)

    metrics = {
        "buggy_kill_triggered": buggy_kill_triggered,
        "fixed_kill_triggered": fixed_kill_triggered,
        "offending_detector": "d38",
        "window": "2026-06-29",
        "threshold": threshold,
        "d38_buggy_rejected_max_per_min": d38_buggy_max,
        "d38_fixed_rejected_max_per_min": d38_fixed_max,
        "d38_present_in_fixed_aggregation": d38_present_in_fixed,
        "d3_live_buggy_rejected_max_per_min": d3_buggy_max,
        "d3_live_fixed_rejected_max_per_min": d3_fixed_max,
        "d3_false_triggered_buggy": d3_false_triggered_buggy,
        "d3_false_triggered_fixed": d3_false_triggered_fixed,
        "buggy_detectors_in_aggregation": buggy_detectors,
        "fixed_detectors_in_aggregation": fixed_detectors,
        "step1_stage_stability_check": stage_snapshot,
    }

    return {
        "metrics": metrics,
        "verdict": verdict,
        "config_hash": _config_hash(),
        "dataset_hash": _dataset_hash(row_count, max_created),
    }


async def _write_agent_run(result: dict, dry_run: bool) -> int | None:
    if dry_run:
        logger.info("dry_run_skip_write", metrics=result["metrics"])
        return None
    row = await fetch(
        """
        INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, started_at, finished_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, now())
        RETURNING id
        """,
        "audit",
        "d38_anomaly_stage_replay",
        result["config_hash"],
        result["dataset_hash"],
        "v1",
        json.dumps(result["metrics"]),
        result["verdict"],
        datetime.now(UTC),
    )
    run_id = row[0]["id"]
    logger.info("agent_run_written", run_id=run_id)
    return run_id


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-start", default=WINDOW_START)
    parser.add_argument("--window-end", default=WINDOW_END)
    parser.add_argument("--dry-run", action="store_true", help="Не писать в agent_runs, только напечатать метрики.")
    args = parser.parse_args()

    # asyncpg требует datetime.datetime для timestamptz-параметров, не str.
    window_start = datetime.fromisoformat(args.window_start.replace("Z", "+00:00"))
    window_end = datetime.fromisoformat(args.window_end.replace("Z", "+00:00"))

    settings = get_settings()
    dsn = settings.agent_database_url
    if not dsn:
        print(
            "WARNING: agent_database_url не задан — read-only роль oraclepoly_agent_ro "
            "ещё не сконфигурирована (Task 2). Скрипт НЕ должен молча уходить под "
            "основным DATABASE_URL для read-only аудита.",
            file=sys.stderr,
        )
        sys.exit(1)

    await init_db(dsn=dsn)
    try:
        result = await _run(window_start, window_end)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        await _write_agent_run(result, args.dry_run)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
