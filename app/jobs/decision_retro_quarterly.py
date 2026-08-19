"""Quarterly-ретро журнала решений — C4 (Phase 2).

Агрегат закрытых ожиданий за квартал по scope×outcome + счётчик открытых.
Шлёт один TG-дайджест всем админам, persist agent_runs(kind='monitoring') всегда.

Пустой журнал — валидный вход: до выхода из shadow кнопки не шлются и ожиданий нет,
дайджест честно рапортует «ожиданий нет» и не падает (спека §4.4).

Design doc: docs/superpowers/specs/2026-07-27-phase2-c4-decision-reconcile-design.md §4.4
Cron: 1-го числа первого месяца квартала 09:00 UTC, вне часов тишины 00:00-08:00.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.analytics.decision_retro import RetroRow, format_retro, quarter_bounds, quarter_label
from app.core.config import get_settings
from app.services.database import execute, fetch, fetchval

logger = structlog.get_logger()

_QUERY_VERSION = "decision_retro_quarterly_v1"
_RUN_NAME = "decision_retro_quarterly:run"

# Агрегат закрытых за квартал: scope берём из payload клика (payload.scope),
# outcome — из результата сверки. JSONB-путь, не JOIN: связь с proposal строковая.
_CLOSED_SQL = """
SELECT payload ->> 'scope'                    AS scope,
       reconciliation_result ->> 'outcome'    AS outcome,
       count(*)                               AS n
  FROM agent_actions
 WHERE reconciled_at >= $1 AND reconciled_at < $2
   AND reconciliation_result IS NOT NULL
 GROUP BY 1, 2
 ORDER BY 1, 2
"""

_OPEN_SQL = """
SELECT count(*) FROM agent_actions WHERE due_date IS NOT NULL AND reconciled_at IS NULL
"""

_INSERT_RUN_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('monitoring', $1, $2, $3, $4, $5::jsonb, $6, now())
"""


async def _send_retro(text: str) -> bool:
    """Один дайджест каждому админу; True — доставлено хотя бы одному."""
    from app.api.telegram import send_telegram_with_keyboard_rows

    settings = get_settings()
    sent = False
    for admin_id in settings.admin_telegram_ids:
        try:
            if await send_telegram_with_keyboard_rows(admin_id, text, []):
                sent = True
        except Exception:
            logger.exception("decision_retro_telegram_failed", admin_id=admin_id)
    return sent


async def _persist_run(summary: dict[str, Any]) -> None:
    await execute(
        _INSERT_RUN_SQL,
        _RUN_NAME,
        f"quarter={summary.get('quarter')}",
        f"closed={summary.get('n_closed', 0)}",
        _QUERY_VERSION,
        json.dumps(summary),
        "ok",
    )


async def run_decision_retro_quarterly() -> dict[str, Any]:
    today = datetime.now(UTC).date()
    start, end = quarter_bounds(today)
    label = quarter_label(today)

    raw = await fetch(_CLOSED_SQL, start, end)
    rows = [RetroRow(scope=r["scope"] or "unknown", outcome=r["outcome"] or "unknown", n=int(r["n"])) for r in raw]
    n_open = int(await fetchval(_OPEN_SQL) or 0)

    text = format_retro(rows, n_open=n_open, label=label)
    tg_sent = await _send_retro(text)
    if not tg_sent:
        logger.error("decision_retro_send_failed_all_admins")

    summary = {
        "status": "ok",
        "quarter": label,
        "n_closed": sum(r.n for r in rows),
        "n_open": n_open,
        "tg_sent": tg_sent,
    }
    await _persist_run(summary)
    logger.info("decision_retro_done", **summary)
    return summary
