"""Tests для callback-обработчика impcard: (Компонент 5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.telegram import CallbackQuery, TelegramUser, _handle_improvement_card_callback


def _make_callback(data: str, user_id: int = 12345) -> CallbackQuery:
    return CallbackQuery(
        id=f"cq_{user_id}",
        **{"from": TelegramUser(id=user_id, is_bot=False, first_name="Admin")},
        data=data,
    )


def _fresh_date() -> str:
    """Дата брифа 'сегодня' — кнопка заведомо не истекла."""
    return datetime.now(UTC).strftime("%Y%m%d")


@pytest.mark.asyncio
async def test_accept_updates_both_fields():
    cq = _make_callback(f"impcard:accept:42:{_fresh_date()}")
    fetchrow_mock = AsyncMock(return_value={"attention_status": "pending"})
    execute_mock = AsyncMock(return_value="UPDATE 1")
    with (
        patch("app.api.telegram.fetchrow", fetchrow_mock),
        patch("app.api.telegram.execute", execute_mock),
        patch("app.api.telegram.answer_callback_query", AsyncMock()) as ack,
    ):
        result = await _handle_improvement_card_callback(cq)
    assert result == {"result": "ok", "card_id": 42}
    # UPDATE несёт оба поля (решение дизайна №1) и идемпотентный guard
    sql = execute_mock.call_args.args[0]
    assert "status" in sql and "attention_status" in sql
    assert "IN ('open', 'deferred')" in sql
    assert execute_mock.call_args.args[1:] == ("accepted", "acted", 42)
    ack.assert_awaited_once()
    assert "42" in ack.call_args.args[1]


@pytest.mark.asyncio
async def test_defer_maps_to_acknowledged():
    cq = _make_callback(f"impcard:defer:7:{_fresh_date()}")
    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value={"attention_status": "pending"})),
        patch("app.api.telegram.execute", AsyncMock(return_value="UPDATE 1")) as execute_mock,
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
    ):
        result = await _handle_improvement_card_callback(cq)
    assert result["result"] == "ok"
    assert execute_mock.call_args.args[1:] == ("deferred", "acknowledged", 7)


@pytest.mark.asyncio
async def test_expired_button_fail_closed():
    old = (datetime.now(UTC) - timedelta(days=8)).strftime("%Y%m%d")
    cq = _make_callback(f"impcard:accept:42:{old}")
    with (
        patch("app.api.telegram.fetchrow", AsyncMock()) as fetchrow_mock,
        patch("app.api.telegram.execute", AsyncMock()) as execute_mock,
        patch("app.api.telegram.answer_callback_query", AsyncMock()) as ack,
    ):
        result = await _handle_improvement_card_callback(cq)
    assert result == {"result": "expired", "card_id": 42}
    fetchrow_mock.assert_not_awaited()  # БД не трогаем вообще
    execute_mock.assert_not_awaited()
    assert "Истекло" in ack.call_args.args[1]


@pytest.mark.asyncio
async def test_bad_payload():
    cq = _make_callback("impcard:fly:42:20260727")
    logger_mock = MagicMock()
    with (
        patch("app.api.telegram.execute", AsyncMock()) as execute_mock,
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.logger", logger_mock),
    ):
        result = await _handle_improvement_card_callback(cq)
    assert result == {"result": "bad_format", "card_id": None}
    execute_mock.assert_not_awaited()
    assert logger_mock.warning.called


@pytest.mark.asyncio
async def test_card_not_found():
    cq = _make_callback(f"impcard:accept:99:{_fresh_date()}")
    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value=None)),
        patch("app.api.telegram.execute", AsyncMock()) as execute_mock,
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
    ):
        result = await _handle_improvement_card_callback(cq)
    assert result == {"result": "not_found", "card_id": 99}
    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_attention_already_handled():
    # attention=acted → next_attention_status бросит ValueError → «уже обработано»
    cq = _make_callback(f"impcard:accept:42:{_fresh_date()}")
    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value={"attention_status": "acted"})),
        patch("app.api.telegram.execute", AsyncMock()) as execute_mock,
        patch("app.api.telegram.answer_callback_query", AsyncMock()) as ack,
    ):
        result = await _handle_improvement_card_callback(cq)
    assert result == {"result": "already_handled", "card_id": 42}
    execute_mock.assert_not_awaited()
    assert "обработан" in ack.call_args.args[1]


@pytest.mark.asyncio
async def test_race_rowcount_zero():
    # Гонка/повтор: guard WHERE status IN ('open','deferred') → UPDATE 0
    cq = _make_callback(f"impcard:accept:42:{_fresh_date()}")
    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value={"attention_status": "pending"})),
        patch("app.api.telegram.execute", AsyncMock(return_value="UPDATE 0")),
        patch("app.api.telegram.answer_callback_query", AsyncMock()) as ack,
    ):
        result = await _handle_improvement_card_callback(cq)
    assert result == {"result": "already_handled", "card_id": 42}
    assert "обработан" in ack.call_args.args[1]


# ── квитанция: перерисовка клавиатуры (решение дизайна №5) ────────────────────


def _make_callback_with_keyboard(data: str, keyboard_rows: list[list[dict]]) -> CallbackQuery:
    return CallbackQuery(
        id="cq_kb",
        **{"from": TelegramUser(id=12345, is_bot=False, first_name="Admin")},
        message={
            "message_id": 777,
            "chat": {"id": 555, "type": "private"},
            "date": 1000000,
            "reply_markup": {"inline_keyboard": keyboard_rows},
        },
        data=data,
    )


def test_drop_card_row_removes_only_target():
    from app.api.telegram import _drop_card_row

    d = _fresh_date()
    rows = [
        [{"text": "a", "callback_data": f"impcard:accept:42:{d}"}, {"text": "d", "callback_data": f"impcard:defer:42:{d}"}],
        [{"text": "a", "callback_data": f"impcard:accept:7:{d}"}, {"text": "d", "callback_data": f"impcard:defer:7:{d}"}],
    ]
    remaining = _drop_card_row(rows, 42)
    assert len(remaining) == 1
    assert all(":7:" in b["callback_data"] for b in remaining[0])
    # id-префиксная коллизия НЕ срабатывает: карточка 4 не матчит ':42:'
    assert _drop_card_row(rows, 4) == rows


@pytest.mark.asyncio
async def test_success_redraws_keyboard_without_card_row():
    d = _fresh_date()
    rows = [
        [{"text": "a", "callback_data": f"impcard:accept:42:{d}"}],
        [{"text": "a", "callback_data": f"impcard:accept:7:{d}"}],
    ]
    cq = _make_callback_with_keyboard(f"impcard:accept:42:{d}", rows)
    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value={"attention_status": "pending"})),
        patch("app.api.telegram.execute", AsyncMock(return_value="UPDATE 1")),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.edit_message_reply_markup", AsyncMock()) as redraw,
    ):
        result = await _handle_improvement_card_callback(cq)
    assert result["result"] == "ok"
    redraw.assert_awaited_once()
    chat_id, message_id, remaining = redraw.call_args.args
    assert (chat_id, message_id) == (555, 777)
    assert len(remaining) == 1 and ":7:" in remaining[0][0]["callback_data"]


@pytest.mark.asyncio
async def test_success_without_message_skips_redraw():
    # Старое сообщение/недоступный контент: message=None → перерисовку молча пропускаем
    cq = _make_callback(f"impcard:accept:42:{_fresh_date()}")  # без message
    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value={"attention_status": "pending"})),
        patch("app.api.telegram.execute", AsyncMock(return_value="UPDATE 1")),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.edit_message_reply_markup", AsyncMock()) as redraw,
    ):
        result = await _handle_improvement_card_callback(cq)
    assert result["result"] == "ok"
    redraw.assert_not_awaited()
