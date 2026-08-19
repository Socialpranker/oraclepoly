"""Tests для app.analytics.improvement_brief — чистый слой, без моков."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.analytics.improvement_brief import (
    BRIEF_ACTIONABLE_CAP,
    BriefCard,
    card_callback_data,
    format_brief,
    is_button_expired,
    parse_card_callback,
    resolve_button_effect,
    select_brief_cards,
)


def _card(card_id: int, ev: float | None, status: str = "open") -> BriefCard:
    return BriefCard(
        id=card_id,
        card_key=f"adverse_selection:detector:D{card_id}",
        title=f"Card {card_id}",
        leak_usd=-100.0 * card_id if ev is not None else None,
        effort_hours=2.0,
        ev_per_hour=ev,
        status=status,
        source_kind="adverse_selection",
    )


# ── select_brief_cards ────────────────────────────────────────────────────────


def test_select_splits_top_and_tail():
    # Вход уже отсортирован SQL-ом (ev DESC NULLS LAST) — функция только режет.
    cards = [_card(1, 500.0), _card(2, 300.0), _card(3, 100.0), _card(4, 50.0), _card(5, None)]
    top, tail = select_brief_cards(cards)
    assert [c.id for c in top] == [1, 2, 3]
    assert [c.id for c in tail] == [4, 5]


def test_select_fewer_than_cap():
    cards = [_card(1, 500.0), _card(2, 300.0)]
    top, tail = select_brief_cards(cards)
    assert len(top) == 2 and tail == []


def test_select_empty():
    assert select_brief_cards([]) == ([], [])


def test_cap_constant_is_three():
    # Контракт из дизайна Компонента 4: кэп ≤3 actionable.
    assert BRIEF_ACTIONABLE_CAP == 3


# ── format_brief ──────────────────────────────────────────────────────────────


def test_format_brief_structure():
    top = [_card(1, 500.0), _card(2, 300.0)]
    tail = [_card(3, 50.0)]
    text = format_brief(top, tail, n_deferred=2, n_rejected_total=1, week_label="21–27.07")
    assert "21–27.07" in text
    assert "Card 1" in text and "Card 2" in text and "Card 3" in text
    assert "500" in text  # EV/час топ-карточки
    assert "Отложено с прошлых недель: 2" in text
    assert "Отклонено навсегда: 1" in text
    # Топ нумерован, хвост — после разделителя дайджеста
    assert text.index("Card 1") < text.index("Card 2") < text.index("Card 3")
    # leak_usd рендерится в топ-блоке (спека §3.1): card 1 leak_usd=-100.0
    card1_block = text[text.index("Card 1") : text.index("Card 2")]
    assert "100" in card1_block


def test_format_brief_ev_none_renders_dash():
    # Карточка без измеренной утечки (shadow) — EV не рисуем числом.
    text = format_brief([], [_card(9, None)], n_deferred=0, n_rejected_total=0, week_label="w")
    assert "Card 9" in text
    assert "None" not in text


# ── callback payload ──────────────────────────────────────────────────────────


def test_callback_roundtrip():
    d = date(2026, 7, 27)
    data = card_callback_data("accept", 42, d)
    assert data == "impcard:accept:42:20260727"
    assert parse_card_callback(data) == ("accept", 42, d)


@pytest.mark.parametrize(
    "bad",
    [
        "impcard:accept:42",  # нет даты
        "impcard:accept:notanint:20260727",
        "impcard:accept:42:notadate",
        "withdraw:approve:abc",  # чужой префикс
        "impcard:fly:42:20260727",  # неизвестный action
        "",
    ],
)
def test_parse_callback_bad_payload(bad):
    assert parse_card_callback(bad) is None


# ── TTL ───────────────────────────────────────────────────────────────────────


def test_button_not_expired_within_ttl():
    brief = date(2026, 7, 27)
    now = datetime(2026, 8, 3, 8, 59, tzinfo=UTC)  # ровно 7 дней — ещё живо
    assert is_button_expired(brief, now) is False


def test_button_expired_after_ttl():
    brief = date(2026, 7, 27)
    now = datetime(2026, 8, 4, 0, 1, tzinfo=UTC)  # 8-й день — истекло
    assert is_button_expired(brief, now) is True


# ── resolve_button_effect ─────────────────────────────────────────────────────


def test_button_effects():
    # Решение дизайна №1: кнопка меняет ОБА поля разом.
    assert resolve_button_effect("accept") == ("accepted", "acted")
    assert resolve_button_effect("defer") == ("deferred", "acknowledged")
    assert resolve_button_effect("reject") == ("rejected", "acted")


def test_button_effect_unknown_raises():
    with pytest.raises(ValueError):
        resolve_button_effect("fly")
