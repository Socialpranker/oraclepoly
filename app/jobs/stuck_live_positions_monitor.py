"""Мониторинг зависших live-позиций — алерт если рынок закрылся (end_date в прошлом)
> STUCK_GRACE_HOURS назад, а позиция всё ещё без резолва и без exit_reason.

Read-only: не трогает торговую логику (max_hold_hours exit остаётся выключенным
per-detector в position_manager._DETECTOR_OVERRIDES, human-decision). Этот монитор
только даёт видимость: без него позиция может висеть неделями незамеченной
(см. инцидент 2026-07-07 — 6 зависших позиций обнаружены постфактум).

Порог — от m.end_date, НЕ от filled_at (фикс 2026-07-07): D3 держит позиции
неделями по design (долгосрочные рынки вроде UAL Q2 revenue), возраст ПОЗИЦИИ
там легитимно велик, пока рынок ещё не закрылся. Значение имеет только возраст
ПОСЛЕ закрытия рынка. Grace 2ч покрывает штатный резолв-пайплайн (market_resolution_poller
*/15 + detector-signals-resolve hourly :40, см. handoff investigate-stuck-live-positions
2026-07-07) — раньше этого молчим, не шумим на нормальной задержке.

Дедуп через Redis (TTL), по order.id — чтобы не спамить на каждый часовой прогон,
пока позиция остаётся открытой.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.analytics.notification import Notification, Severity
from app.services.database import fetch
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

STUCK_GRACE_HOURS: float = 2.0
ALERT_DEDUP_TTL_SEC: int = 24 * 3600  # повторный алерт по той же позиции не чаще раза в 24ч

_STUCK_LIVE_POSITIONS_SQL = """
SELECT
    eo.id,
    eo.detector,
    eo.market_id,
    eo.side,
    eo.stake::float AS stake,
    eo.filled_at,
    m.end_date
  FROM execution_orders eo
  JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status         = 'filled'
   AND eo.execution_mode IN ('maker', 'taker')
   AND eo.exit_reason   IS NULL
   AND m.resolved        = false
   AND m.end_date        < now() - make_interval(hours => $1)
 ORDER BY m.end_date ASC
"""


def _fmt_hours(value: float | None) -> str:
    """Часы в текст. None → «неизвестно», а не падение на форматировании.

    filled_at и end_date приходят из БД и могут быть NULL: SQL их ненулевыми
    не гарантирует. Раньше None доезжал до f-строки с `:.1f` и ронял джобу
    вместо того, чтобы прислать алерт о зависшей позиции — то есть монитор
    молчал ровно в том случае, ради которого он написан.
    """
    return "неизвестно" if value is None else f"{value:.1f}ч"


def to_notifications(stuck: list[dict[str, Any]]) -> list[Notification]:
    """Зависшие live-позиции → Notification (спека §3).

    По одному уведомлению на позицию, а не общий список: subject у каждой свой
    (`order:<id>`), и дедуп шины работает по позиции. Слить их в одно сообщение
    значило бы, что вторая зависшая позиция подавится cooldown'ом первой.

    Severity ACTION_REQUIRED: рынок закрылся, деньги стоят в позиции, и без
    вмешательства они там и останутся — это ровно тот случай, для которого
    уровень заведён.
    """
    out: list[Notification] = []
    for row in stuck:
        order_id = str(row["id"])
        stake = float(row["stake"] or 0.0)
        out.append(
            Notification(
                severity=Severity.ACTION_REQUIRED,
                subject=f"order:{order_id}",
                kind="stuck_live_position",
                headline="Live-позиция зависла после закрытия рынка",
                what_happened=[
                    f"Детектор {row['detector']}, сторона {row['side']}, ставка ${stake:.2f}.",
                    f"Рынок закрылся {_fmt_hours(row.get('hours_since_end'))} назад, "
                    f"резолва и причины выхода до сих пор нет.",
                    f"В позиции {_fmt_hours(row.get('age_hours'))}.",
                ],
                # Цену бездействия не выдумываем (§3): исход рынка неизвестен,
                # и ставка — это не убыток, а замороженные деньги.
                what_if_ignored=(f"${stake:.2f} остаются заморожены в позиции, пока рынок не резолвнется."),
                what_to_do=(
                    "Проверь market_resolution_poller и состояние рынка на Polymarket: "
                    "либо резолв не доехал, либо рынок закрыт без исхода."
                ),
                actions=[],
                evidence=[
                    f"order_id={order_id}",
                    f"market_id={row['market_id']}",
                    f"stake=${stake:.2f}",
                ],
            )
        )
    return out


async def run_stuck_live_positions_monitor() -> dict[str, Any]:
    """Cron entry point (hourly). Алерт при live-позиции, чей рынок закрылся > STUCK_GRACE_HOURS назад."""
    from app.core.cache import get_redis

    rows = await fetch(_STUCK_LIVE_POSITIONS_SQL, STUCK_GRACE_HOURS)
    summary: dict[str, Any] = {"stuck_count": len(rows), "alerted": []}

    if not rows:
        logger.info("stuck_live_positions_none")
        return summary

    r = await get_redis()
    to_send: list[dict[str, Any]] = []
    for row in rows:
        order_id = str(row["id"])
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        age_hours = (now - row["filled_at"]).total_seconds() / 3600.0 if row["filled_at"] else None
        hours_since_end = (now - row["end_date"]).total_seconds() / 3600.0 if row["end_date"] else None

        # Свой дедуп джобы остаётся (решение владельца по очереди 1): джоба сама
        # решает, слать ли, и зовёт шину только когда решила да. Cooldown шины —
        # второй рубеж, а не замена этому.
        dedup_key = f"stuck_live_position_alert:{order_id}"
        send = True
        if r is not None:
            try:
                send = not await r.exists(dedup_key)
            except Exception as e:
                logger.debug("stuck_live_position_dedup_error", error=str(e)[:80])

        if send:
            to_send.append({**row, "age_hours": age_hours, "hours_since_end": hours_since_end})

        logger.warning(
            "stuck_live_position",
            order_id=order_id,
            detector=row["detector"],
            age_hours=round(age_hours, 1) if age_hours is not None else None,
            hours_since_end=round(hours_since_end, 1) if hours_since_end is not None else None,
            alerted=send,
        )

    if not to_send:
        return summary

    result = await publish(to_notifications(to_send), actor="stuck_live_positions_monitor")
    summary["published"] = str(result)

    # Ключ дедупа ставим ПОСЛЕ публикации, а не до: раньше он выставлялся перед
    # отправкой, и упавшая отправка означала молчание про зависшие деньги на
    # ближайшие 24ч. Лишний повтор здесь дешевле потерянного алерта.
    for row in to_send:
        if r is not None:
            try:
                await r.set(f"stuck_live_position_alert:{row['id']}", "1", ex=ALERT_DEDUP_TTL_SEC)
            except Exception as e:
                logger.debug("stuck_live_position_dedup_set_error", error=str(e)[:80])
        summary["alerted"].append(str(row["id"]))

    return summary
