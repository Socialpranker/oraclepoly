"""Tests для TG-хелперов разметки рядов (Компонент 5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.api.telegram import edit_message_reply_markup, send_telegram_with_keyboard_rows


def _make_settings(token: str | None = "tok") -> object:
    return type("S", (), {"telegram_bot_token": token})()


def _client_mock() -> tuple[MagicMock, AsyncMock]:
    """Mock httpx.AsyncClient как async context manager; возвращает (класс, post-мок)."""
    post_mock = AsyncMock(return_value=MagicMock(raise_for_status=lambda: None))
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=post_mock))
    client.__aexit__ = AsyncMock(return_value=False)
    client_cls = MagicMock(return_value=client)
    return client_cls, post_mock


@pytest.mark.asyncio
async def test_send_keyboard_rows_passes_rows_as_is():
    rows = [
        [{"text": "A", "callback_data": "impcard:accept:1:20260727"}],
        [{"text": "B1", "callback_data": "x"}, {"text": "B2", "callback_data": "y"}],
    ]
    client_cls, post_mock = _client_mock()
    with (
        patch("app.api.telegram.get_settings", lambda: _make_settings()),
        patch("app.api.telegram.httpx.AsyncClient", client_cls),
    ):
        result = await send_telegram_with_keyboard_rows(123, "text", rows)
    payload = post_mock.call_args.kwargs["json"]
    # Ряды уходят как есть — БЕЗ переупаковки [[btn] for btn in ...]
    assert payload["reply_markup"]["inline_keyboard"] == rows
    assert payload["chat_id"] == 123
    assert result is True


@pytest.mark.asyncio
async def test_send_keyboard_rows_no_token_is_noop():
    client_cls, post_mock = _client_mock()
    with (
        patch("app.api.telegram.get_settings", lambda: _make_settings(token=None)),
        patch("app.api.telegram.httpx.AsyncClient", client_cls),
    ):
        result = await send_telegram_with_keyboard_rows(123, "text", [])
    post_mock.assert_not_called()
    assert result is False


@pytest.mark.asyncio
async def test_send_keyboard_rows_http_error_returns_false():
    def _raise_status():
        raise httpx.HTTPStatusError("boom", request=MagicMock(), response=MagicMock())

    post_mock = AsyncMock(return_value=MagicMock(raise_for_status=_raise_status))
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=MagicMock(post=post_mock))
    client.__aexit__ = AsyncMock(return_value=False)
    client_cls = MagicMock(return_value=client)
    with (
        patch("app.api.telegram.get_settings", lambda: _make_settings()),
        patch("app.api.telegram.httpx.AsyncClient", client_cls),
    ):
        result = await send_telegram_with_keyboard_rows(123, "text", [])
    assert result is False


@pytest.mark.asyncio
async def test_edit_reply_markup_calls_bot_api():
    rows = [[{"text": "A", "callback_data": "x"}]]
    client_cls, post_mock = _client_mock()
    with (
        patch("app.api.telegram.get_settings", lambda: _make_settings()),
        patch("app.api.telegram.httpx.AsyncClient", client_cls),
    ):
        await edit_message_reply_markup(123, 456, rows)
    url = post_mock.call_args.args[0]
    payload = post_mock.call_args.kwargs["json"]
    assert url.endswith("/editMessageReplyMarkup")
    assert payload == {
        "chat_id": 123,
        "message_id": 456,
        "reply_markup": {"inline_keyboard": rows},
    }
