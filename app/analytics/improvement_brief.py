"""Improvement Brief — чистый слой Компонента 5 (Phase 1 Portfolio Analyst Agent).

Выборка топ/хвост по кэпу, рендер текста брифа (TG-спека §4.8), payload кнопок
с вычисляемым TTL, маппинг кнопка → (status, attention_event). Без DB и side
effects — по образцу app/analytics/improvement_cards.py.

Design doc: docs/superpowers/specs/2026-07-22-improvement-brief-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

# Контракт из дизайна Компонента 4: ≤3 actionable-блока в день;
# один weekly-бриф несёт ровно кэп кнопочных карточек.
BRIEF_ACTIONABLE_CAP = 3
# TTL кнопок — до следующего брифа (решение дизайна №2, fail-closed §7.2 TG-спеки).
BUTTON_TTL_DAYS = 7

_CALLBACK_PREFIX = "impcard"
_ACTIONS = ("accept", "defer", "reject")

# Решение дизайна №1: кнопка меняет оба поля разом.
_BUTTON_EFFECTS: dict[str, tuple[str, str]] = {
    "accept": ("accepted", "acted"),
    "defer": ("deferred", "acknowledged"),
    "reject": ("rejected", "acted"),
}


@dataclass(frozen=True)
class BriefCard:
    """Строка improvement_cards в объёме, нужном брифу."""

    id: int
    card_key: str
    title: str
    leak_usd: float | None
    effort_hours: float | None
    ev_per_hour: float | None
    status: str
    source_kind: str


def select_brief_cards(
    cards: list[BriefCard], cap: int = BRIEF_ACTIONABLE_CAP
) -> tuple[list[BriefCard], list[BriefCard]]:
    """Разбить отсортированный список на (топ с кнопками, хвост-дайджест).

    Сортировка ev_per_hour DESC NULLS LAST уже сделана SQL-ом — функция только режет.
    """
    return cards[:cap], cards[cap:]


def _ev_label(card: BriefCard) -> str:
    if card.ev_per_hour is None:
        return "EV не измерен"
    return f"EV ${card.ev_per_hour:,.0f}/час"


def _effort_label(card: BriefCard) -> str:
    if card.effort_hours is None:
        return ""
    return f" · ~{card.effort_hours:g} ч"


def _leak_label(card: BriefCard) -> str:
    if card.leak_usd is None:
        return ""
    return f" · утечка ${abs(card.leak_usd):,.0f}"


def format_brief(
    top: list[BriefCard],
    tail: list[BriefCard],
    n_deferred: int,
    n_rejected_total: int,
    week_label: str,
) -> str:
    """Рендер текста брифа по шаблону agent_tg_communication.md §4.8.

    Топ — нумерованные блоки (кнопки добавляет джоба клавиатурой),
    хвост — дайджест-строки, затем счётчики.
    """
    lines: list[str] = [f"🔵 Куда следующие 10 часов — неделя {week_label}."]
    lines.append("")
    for i, card in enumerate(top, start=1):
        lines.append(f"{i}. {card.title}")
        lines.append(f"   {_ev_label(card)}{_effort_label(card)}{_leak_label(card)}")
    if tail:
        lines.append("")
        lines.append("Дайджест (без кнопок):")
        for card in tail:
            lines.append(f"• {card.title} — {_ev_label(card)}")
    lines.append("")
    lines.append(
        f"Отложено с прошлых недель: {n_deferred} (перенесены в этот список автоматически). "
        f"Отклонено навсегда: {n_rejected_total}."
    )
    lines.append("_ранжирование: EV на час твоего времени_")
    return "\n".join(lines)


def card_callback_data(action: str, card_id: int, brief_date: date) -> str:
    """'impcard:accept:42:20260727' — дата брифа в payload задаёт TTL (решение №4)."""
    if action not in _ACTIONS:
        raise ValueError(f"unknown brief action {action!r}")
    return f"{_CALLBACK_PREFIX}:{action}:{card_id}:{brief_date.strftime('%Y%m%d')}"


def parse_card_callback(data: str) -> tuple[str, int, date] | None:
    """Разбор payload кнопки. None на любом битом формате (роутер ответит toast'ом)."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != _CALLBACK_PREFIX or parts[1] not in _ACTIONS:
        return None
    try:
        card_id = int(parts[2])
        brief_date = datetime.strptime(parts[3], "%Y%m%d").date()
    except ValueError:
        return None
    return parts[1], card_id, brief_date


def is_button_expired(brief_date: date, now: datetime, ttl_days: int = BUTTON_TTL_DAYS) -> bool:
    """TTL истёк → fail-closed (§7.2): кнопка мертва, карточку не трогаем."""
    return now.date() > brief_date + timedelta(days=ttl_days)


def resolve_button_effect(action: str) -> tuple[str, str]:
    """action → (new_status, attention_event). Решение дизайна №1: оба поля разом."""
    try:
        return _BUTTON_EFFECTS[action]
    except KeyError:
        raise ValueError(f"unknown brief action {action!r}") from None
