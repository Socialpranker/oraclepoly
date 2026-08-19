"""Маршрутизация уведомлений — спека 2026-08-04-notification-envelope-design.md §6.

Единственное место, где Notification превращается в отправку. Дедуп, склейка,
создание agent_actions по контракту Phase 3 — здесь, а не в 30 джобах.

Дедуп time-based по стабильному ключу (subject|kind). Календарное окно в ключе
запрещено: оно обнуляет cooldown на границе окна — инцидент 2026-08-01 (PR #741).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace

import structlog

from app.analytics.action_gates import ALLOWED_KINDS, snapshot_hash
from app.analytics.notification import Notification, Severity
from app.analytics.notification_render import render, render_digest_line
from app.api.telegram import send_telegram_with_keyboard_rows
from app.core.config import get_settings
from app.services.database import execute, fetchrow

logger = structlog.get_logger(__name__)

_COOLDOWN_SQL = """
SELECT last_sent_at FROM notification_log
 WHERE dedup_key = $1 AND last_sent_at > now() - ($2 || ' hours')::interval
 LIMIT 1
"""

_MARK_SENT_SQL = """
INSERT INTO notification_log (dedup_key, last_sent_at)
VALUES ($1, now())
ON CONFLICT (dedup_key) DO UPDATE SET last_sent_at = now()
"""

_INSERT_ACTION_SQL = """
INSERT INTO agent_actions
    (kind, level, status, actor, payload, payload_hash, idempotency_key, expires_at)
VALUES ($1, 'L2', 'proposed', $2, $3::jsonb, $4, $5, now() + interval '24 hours')
ON CONFLICT (idempotency_key) DO NOTHING
"""

_INSERT_FYI_SQL = """
INSERT INTO notification_fyi_queue (subject, kind, actor, headline, digest_line)
VALUES ($1, $2, $3, $4, $5)
"""


@dataclass(frozen=True)
class PublishResult:
    sent: int = 0
    suppressed: int = 0
    digested: int = 0
    failures: int = 0


def _pid_for(subject: str, kind: str, actor: str, cooldown_hours: int) -> str:
    """Стабильный id предложения. Совпадает по длине с pid advisory (16 hex).

    CRITICAL 1 (ревью 85a68b2a): ключ обязан включать `kind` — иначе два разных
    по содержанию уведомления от одного actor/subject (разный kind, либо то же
    самое после истечения cooldown) схлопываются в один idempotency_key,
    ON CONFLICT DO NOTHING молча топит второй INSERT, и свежая кнопка ссылается
    на протухшую вчерашнюю строку agent_actions.

    Временная компонента — скользящий bucket по номеру cooldown-интервала от
    эпохи, шириной ровно в cooldown_hours (не календарная дата: без привязки
    к полуночи/дню недели, поэтому не воспроизводит инцидент PR #741).
    `_pid_for` вызывается только ПОСЛЕ прохождения cooldown-проверки в
    publish(), то есть только на первой отправке окна — bucket даёт разные pid
    между окнами и не обязан быть стабильным внутри окна, т.к. повторный вызов
    в том же окне до сюда не доходит."""
    bucket = int(time.time() // (cooldown_hours * 3600))
    return hashlib.sha256(f"{actor}|{subject}|{kind}|{bucket}".encode()).hexdigest()[:16]


def _merge(items: list[Notification]) -> Notification:
    """Склейка однопредметных уведомлений (спека §3): блоки конкатенируются,
    заголовок берётся у первого. Чинит дубль D38 из §1 п.4.

    MINOR 4 (ревью 85a68b2a): severity результата — severity первого элемента,
    без проверки остальных. Через publish() смешение ACTION_REQUIRED и FYI
    сюда никогда не доходит (FYI отфильтрован в publish() до группировки по
    subject), но _merge() — публичная функция модуля, и это её инвариант:
    вызывающий обязан гарантировать однородность severity в items ДО вызова.
    Список деклараций разной severity здесь — ошибка вызывающего кода, не
    поддерживаемый кейс склейки."""
    if len(items) == 1:
        return items[0]
    assert all(i.severity == items[0].severity for i in items), (
        "_merge() требует однородный severity — смешение фильтруется в publish() ДО группировки"
    )
    head = items[0]
    return Notification(
        severity=head.severity,
        subject=head.subject,
        kind="+".join(sorted({i.kind for i in items})),
        headline=head.headline,
        what_happened=[line for i in items for line in i.what_happened],
        what_if_ignored=next((i.what_if_ignored for i in items if i.what_if_ignored), None),
        what_to_do=next((i.what_to_do for i in items if i.what_to_do), None),
        actions=[a for i in items for a in i.actions],
        evidence=[e for i in items for e in i.evidence],
    )


async def _create_action(n: Notification, pid: str, actor: str) -> bool:
    """Заводит L2-строку по контракту Phase 3. Неизвестный kind отклоняется
    ДО вставки (риск №3 Phase 3), сообщение при этом уходит без кнопки."""
    for spec in n.actions:
        if spec.kind not in ALLOWED_KINDS:
            logger.warning("notification_action_kind_rejected", kind=spec.kind, subject=n.subject)
            return False
        await execute(
            _INSERT_ACTION_SQL,
            spec.kind,
            actor,
            json.dumps(dict(spec.payload), sort_keys=True),
            snapshot_hash(spec.snapshot),
            pid,
        )
        return True
    return False


async def publish(notifications: list[Notification], actor: str, cooldown_hours: int = 25) -> PublishResult:
    """Маршрутизация по severity. cooldown 25ч by default: окно обязано
    перекрывать суточный интервал джобы (урок PR #741, 24ч = ровно граница).

    IMPORTANT 3 (ревью 85a68b2a): порядок записи agent_actions и notification_log
    выбран НАМЕРЕННО асимметричным, оба риска осознаны:

    - agent_actions создаётся ДО отправки в Telegram (_create_action раньше
      send_telegram_with_keyboard_rows). Риск: если процесс упадёт между
      вставкой строки и отправкой, останется висячая строка agent_actions в
      статусе 'proposed', на которую никто никогда не нажмёт — она просто
      истечёт через 24 часа (expires_at). Цена — мусорная строка в таблице,
      без действия и без дублирования сообщения.
    - notification_log пишется ПОСЛЕ успешной отправки (_MARK_SENT_SQL в конце
      цикла). Риск: если процесс упадёт МЕЖДУ отправкой и записью cooldown,
      следующий прогон не увидит cooldown и отправит дубль — но дубль получит
      новый pid (см. _pid_for: bucket по времени), так что кнопка в дубле
      рабочая, просто пользователь увидит два одинаковых по смыслу сообщения.

    Выбор в пользу этого порядка: сбой ДО отправки не должен блокировать
    cooldown (иначе один сбойный прогон подавляет реальное уведомление на
    25 часов молча), а сбой ПОСЛЕ отправки — не должен оставлять кнопку без
    парной строки agent_actions (кликабельная кнопка, ведущая в никуда, хуже
    редкого дубля). Итого: at-least-once по notification_log, at-most-one-
    dangling-row по agent_actions — асимметрия в пользу того, чтобы сбой был
    шумным дублем, а не тихой потерей действия с деньгами."""
    sent = suppressed = digested = failures = 0

    by_subject: dict[str, list[Notification]] = {}
    for n in notifications:
        if n.severity == Severity.TELEMETRY:
            continue
        if n.severity == Severity.FYI:
            # Сохраняем ДО инкремента счётчика: digested обязан означать «доехало
            # до дайджеста», а не «мы его видели». Сбой записи не валит публикацию
            # остальной пачки — FYI справочный, а ACTION_REQUIRED рядом несёт
            # решение о деньгах и должен уйти в любом случае.
            try:
                await execute(
                    _INSERT_FYI_SQL,
                    n.subject,
                    n.kind,
                    actor,
                    n.headline,
                    render_digest_line(n),
                )
                digested += 1
            except Exception as exc:
                failures += 1
                logger.warning("notification_fyi_persist_failed", subject=n.subject, error=str(exc))
            continue
        by_subject.setdefault(n.subject, []).append(n)

    admin_ids = get_settings().admin_telegram_ids

    for subject, items in by_subject.items():
        merged = _merge(items)
        key = merged.dedup_key()

        if await fetchrow(_COOLDOWN_SQL, key, str(cooldown_hours)) is not None:
            suppressed += 1
            logger.info("notification_suppressed_cooldown", dedup_key=key)
            continue

        pid = _pid_for(subject, merged.kind, actor, cooldown_hours)
        # Кнопка «⚡ Сделать» рисуется только когда парная строка agent_actions
        # реально создана: иначе клик уходил бы в никуда (паттерн has_l2 из C3b).
        has_action = await _create_action(merged, pid, actor)
        for_render = merged if has_action else replace(merged, actions=())
        text, rows = render(for_render, pid=pid)

        delivered_count = 0
        for admin_id in admin_ids:
            if await send_telegram_with_keyboard_rows(admin_id, text, rows):
                delivered_count += 1
            else:
                failures += 1

        # CRITICAL 2 (ревью 85a68b2a): cooldown пишем только при доставке ВСЕМ
        # админам, не при "хотя бы одному". Цена ошибки асимметрична: не записать
        # cooldown при частичном сбое стоит дубль тем, кто уже получил — но дубль
        # безвреден, т.к. pid/idempotency_key детерминирован (тот же bucket) и
        # ON CONFLICT DO NOTHING не даст вторую строку agent_actions. Записать
        # cooldown при частичной доставке стоит ДРУГОЕ: админ, до которого
        # сообщение не дошло, не увидит ACTION_REQUIRED с денежным действием
        # следующие 25 часов — молча и без права на исправление до истечения
        # окна. При объёме 0-2 ACTION_REQUIRED/день шумный дубль дешевле тихой
        # потери решения о деньгах.
        if admin_ids and delivered_count == len(admin_ids):
            sent += 1
            await execute(_MARK_SENT_SQL, key)
        elif delivered_count > 0:
            sent += 1
            logger.warning(
                "notification_partial_delivery_no_cooldown",
                dedup_key=key,
                delivered=delivered_count,
                total=len(admin_ids),
            )

    return PublishResult(sent=sent, suppressed=suppressed, digested=digested, failures=failures)
