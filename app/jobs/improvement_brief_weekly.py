"""Weekly Improvement Brief — weekly cron (Phase 1, Компонент 5 Portfolio Analyst Agent).

Читает open+deferred карточки improvement_cards (digest/actionable), шлёт один
TG-бриф всем админам: топ-3 по EV/час с кнопками «В работу / Отложить / Отклонить»
(клавиатура 3 ряда × 3 кнопки), хвост — дайджест-строками. Кнопки обрабатывает
callback-ветка impcard: в app/api/telegram.py. Persist agent_runs всегда.

Design doc: docs/superpowers/specs/2026-07-22-improvement-brief-design.md
Cron: weekly пн 09:00 UTC (`0 9 * * 1`), после improvement-cards-monitor (07:30),
вне часов тишины 00:00–08:00 (agent_tg_communication.md §3).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from app.analytics.improvement_brief import (
    BriefCard,
    card_callback_data,
    format_brief,
    select_brief_cards,
)
from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()

_QUERY_VERSION = "improvement_brief_weekly_v1"
_RUN_NAME = "improvement_brief_weekly:run"

# Решение дизайна №3: open+deferred × digest/actionable, топ по EV/час.
# deferred возвращаются автоматически (решение №2). NULLS LAST — shadow в хвост.
_BRIEF_CARDS_SQL = """
SELECT id, card_key, title, leak_usd, effort_hours, ev_per_hour, status, source_kind
FROM improvement_cards
WHERE status IN ('open', 'deferred')
  AND lifecycle_stage IN ('digest', 'actionable')
ORDER BY ev_per_hour DESC NULLS LAST, id
"""

_REJECTED_TOTAL_SQL = "SELECT count(*) AS n FROM improvement_cards WHERE status = 'rejected'"

_INSERT_RUN_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('monitoring', $1, $2, $3, $4, $5::jsonb, $6, now())
"""

_BUTTON_LABELS = [("accept", "▶️ #{card_id} в работу"), ("defer", "⏸ #{card_id}"), ("reject", "❌ #{card_id}")]


def _week_label(monday: date) -> str:
    """'27.07–02.08' — неделя от даты брифа (понедельник) включительно."""
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime('%d.%m')}–{sunday.strftime('%d.%m')}"


def _build_keyboard_rows(top: list[BriefCard], brief_date: date) -> list[list[dict]]:
    """Ряд на карточку: 3 кнопки-действия. Номер карточки в тексте кнопки,
    чтобы владелец видел соответствие с нумерованным блоком текста."""
    rows: list[list[dict]] = []
    for card in top:
        rows.append(
            [
                {
                    "text": label.format(card_id=card.id),
                    "callback_data": card_callback_data(action, card.id, brief_date),
                }
                for action, label in _BUTTON_LABELS
            ]
        )
    return rows


async def _fetch_brief_cards() -> list[BriefCard]:
    rows = await fetch(_BRIEF_CARDS_SQL)
    return [
        BriefCard(
            id=r["id"],
            card_key=r["card_key"],
            title=r["title"],
            leak_usd=float(r["leak_usd"]) if r["leak_usd"] is not None else None,
            effort_hours=float(r["effort_hours"]) if r["effort_hours"] is not None else None,
            ev_per_hour=float(r["ev_per_hour"]) if r["ev_per_hour"] is not None else None,
            status=r["status"],
            source_kind=r["source_kind"],
        )
        for r in rows
    ]


async def _fetch_rejected_total() -> int:
    row = await fetchrow(_REJECTED_TOTAL_SQL)
    return int(row["n"]) if row else 0


async def _send_brief(text: str, keyboard_rows: list[list[dict]]) -> bool:
    """Разослать бриф всем админам. True если ушёл хотя бы одному."""
    from app.api.telegram import send_telegram_with_keyboard_rows
    from app.core.config import get_settings

    settings = get_settings()
    sent = False
    for admin_id in settings.admin_telegram_ids:
        try:
            ok = await send_telegram_with_keyboard_rows(admin_id, text, keyboard_rows)
            sent = sent or bool(ok)
        except Exception:
            logger.exception("improvement_brief_telegram_failed", admin_id=admin_id)
    return sent


async def _persist_run(summary: dict[str, Any]) -> None:
    config_hash = f"cap={summary.get('n_top', 0)}"
    dataset_hash = f"cards={summary.get('n_cards', 0)}"
    await execute(
        _INSERT_RUN_SQL,
        _RUN_NAME,
        config_hash,
        dataset_hash,
        _QUERY_VERSION,
        json.dumps(summary),
        "ok",
    )


async def run_improvement_brief_weekly() -> dict[str, Any]:
    today = datetime.now(UTC).date()
    cards = await _fetch_brief_cards()
    n_rejected_total = await _fetch_rejected_total()

    if not cards:
        summary: dict[str, Any] = {
            "status": "no_cards",
            "n_cards": 0,
            "n_top": 0,
            "n_tail": 0,
            "n_deferred": 0,
            "n_rejected_total": n_rejected_total,
            "top_ev": None,
            "tg_sent": False,
        }
        logger.info("improvement_brief_no_cards")
        await _persist_run(summary)
        return summary

    top, tail = select_brief_cards(cards)
    n_deferred = sum(1 for c in cards if c.status == "deferred")
    text = format_brief(
        top, tail, n_deferred=n_deferred, n_rejected_total=n_rejected_total, week_label=_week_label(today)
    )
    keyboard_rows = _build_keyboard_rows(top, today)

    tg_sent = await _send_brief(text, keyboard_rows)
    if not tg_sent:
        logger.error("improvement_brief_send_failed_all_admins")

    top_ev = top[0].ev_per_hour if top and top[0].ev_per_hour is not None else None
    summary = {
        "status": "ok",
        "n_cards": len(cards),
        "n_top": len(top),
        "n_tail": len(tail),
        "n_deferred": n_deferred,
        "n_rejected_total": n_rejected_total,
        "top_ev": top_ev,
        "tg_sent": tg_sent,
    }
    await _persist_run(summary)
    return summary
