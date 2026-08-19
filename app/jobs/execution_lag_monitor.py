"""Лаг-мониторинг signal→order по first-row p50 (Fix-2, latency-аудит №3).

Лаг исполнения = первая строка execution_orders на сигнал (первая по
created_at per detector_signal_id) минус detector_signals.generated_at.
Именно первая строка: transient-reject ретраи плодят повторные строки того же
сигнала с поздними created_at и раздувают avg в ~40× (вердикт аудита —
research/fable-goal-min-latency-2026-07-06/answer.md; D3-демон first-row p50
0.35с при avg 14.9с из retry-строк).

Разбивка по meta->>'source' (daemon/cron, Fix-2 source-tag) показывает
реальный выигрыш демона против минутного cron-watchdog. Строки, вставленные
до деплоя source-tag, попадают в 'unknown'.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.database import fetch

logger = structlog.get_logger(__name__)

_WINDOW_HOURS = 24

_LAG_SQL = """
WITH first_rows AS (
    SELECT DISTINCT ON (eo.detector_signal_id)
        eo.detector_signal_id,
        eo.created_at AS first_row_at,
        COALESCE(eo.meta->>'source', 'unknown') AS source
    FROM execution_orders eo
    WHERE eo.created_at > now() - interval '24 hours'
      AND eo.detector_signal_id IS NOT NULL
    ORDER BY eo.detector_signal_id, eo.created_at
)
SELECT
    fr.source,
    COUNT(*)::int AS n_signals,
    PERCENTILE_CONT(0.5) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (fr.first_row_at - ds.generated_at))
    )::float AS lag_p50_sec,
    PERCENTILE_CONT(0.95) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (fr.first_row_at - ds.generated_at))
    )::float AS lag_p95_sec
FROM first_rows fr
JOIN detector_signals ds ON ds.id = fr.detector_signal_id
WHERE ds.generated_at IS NOT NULL
GROUP BY fr.source
ORDER BY fr.source
"""


async def run_execution_lag_monitor() -> dict[str, Any]:
    """Cron entry point (hourly). Логирует first-row p50/p95 per source."""
    rows = await fetch(_LAG_SQL)
    sources: dict[str, dict[str, Any]] = {}
    for r in rows:
        stats = {
            "n_signals": int(r["n_signals"]),
            "lag_p50_sec": round(float(r["lag_p50_sec"]), 2) if r["lag_p50_sec"] is not None else None,
            "lag_p95_sec": round(float(r["lag_p95_sec"]), 2) if r["lag_p95_sec"] is not None else None,
        }
        sources[r["source"]] = stats
        logger.info("execution_lag_first_row", source=r["source"], **stats)
    if not sources:
        logger.info("execution_lag_first_row_empty", window_hours=_WINDOW_HOURS)
    return {"window_hours": _WINDOW_HOURS, "sources": sources}
