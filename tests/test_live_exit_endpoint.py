"""Live exit — REST endpoint + TG callback dispatch (audit топ-1, 2026-07-02).

POST /api/bot/v2/live/close/{order_id} и telegram.py close_pos: callback — оба
аварийных пути закрытия live-позиции, оба делегируют в close_live_position.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.bot import router as bot_router
from app.core.auth import check_admin


@pytest.fixture
def app_with_bot():
    app = FastAPI()
    app.include_router(bot_router, prefix="/api/bot")
    return app


@pytest.fixture
def app_with_bot_admin_override():
    app = FastAPI()
    app.include_router(bot_router, prefix="/api/bot")
    app.dependency_overrides[check_admin] = lambda: {"method": "bearer", "admin_id": "api_key"}
    return app


class TestLiveCloseEndpointAuth:
    def test_close_without_bearer_403(self, app_with_bot):
        client = TestClient(app_with_bot)
        response = client.post("/api/bot/v2/live/close/order-123")
        assert response.status_code == 403


class TestLiveCloseEndpoint:
    def test_close_success_returns_exit_order_id(self, app_with_bot_admin_override):
        mock_close = AsyncMock(return_value=(True, "sell-oid-1"))
        with patch("app.services.live_exit.close_live_position", mock_close):
            client = TestClient(app_with_bot_admin_override)
            response = client.post("/api/bot/v2/live/close/order-123")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["exit_order_id"] == "sell-oid-1"
        mock_close.assert_awaited_once_with("order-123", reason="manual_api")

    def test_close_failure_returns_ok_false_with_error(self, app_with_bot_admin_override):
        mock_close = AsyncMock(return_value=(False, "no_book_for_exit"))
        with patch("app.services.live_exit.close_live_position", mock_close):
            client = TestClient(app_with_bot_admin_override)
            response = client.post("/api/bot/v2/live/close/order-123")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["error"] == "no_book_for_exit"


class TestTelegramClosePosCallback:
    """telegram.py close_pos: callback branch — не FastAPI endpoint, тестируем dispatch напрямую."""

    @pytest.mark.asyncio
    async def test_close_pos_callback_success(self, monkeypatch):
        from app.api import telegram as tg

        answered = []

        async def mock_answer_callback_query(cq_id, text=None):
            answered.append((cq_id, text))

        async def mock_close_live_position(order_id, reason):
            return True, "sell-oid-1"

        class _FakeFrom:
            id = 1  # admin

        class _FakeCQ:
            id = "cq-1"
            data = "close_pos:order-db-1"
            from_ = _FakeFrom()
            message = None

        def _fake_settings():
            class _S:
                admin_telegram_ids = [1]

            return _S()

        monkeypatch.setattr(tg, "get_settings", _fake_settings)
        monkeypatch.setattr(tg, "answer_callback_query", mock_answer_callback_query)
        monkeypatch.setattr("app.services.live_exit.close_live_position", mock_close_live_position)

        await tg._handle_callback_query(_FakeCQ())

        assert answered == [("cq-1", "🔴 SELL отправлен")]

    @pytest.mark.asyncio
    async def test_close_pos_callback_failure_shows_reason(self, monkeypatch):
        from app.api import telegram as tg

        answered = []

        async def mock_answer_callback_query(cq_id, text=None):
            answered.append((cq_id, text))

        async def mock_close_live_position(order_id, reason):
            return False, "not_closeable:exiting"

        class _FakeFrom:
            id = 1

        class _FakeCQ:
            id = "cq-2"
            data = "close_pos:order-db-2"
            from_ = _FakeFrom()
            message = None

        def _fake_settings():
            class _S:
                admin_telegram_ids = [1]

            return _S()

        monkeypatch.setattr(tg, "get_settings", _fake_settings)
        monkeypatch.setattr(tg, "answer_callback_query", mock_answer_callback_query)
        monkeypatch.setattr("app.services.live_exit.close_live_position", mock_close_live_position)

        await tg._handle_callback_query(_FakeCQ())

        assert answered == [("cq-2", "❌ not_closeable:exiting")]

    @pytest.mark.asyncio
    async def test_close_pos_callback_rejects_non_admin(self, monkeypatch):
        from app.api import telegram as tg

        answered = []

        async def mock_answer_callback_query(cq_id, text=None):
            answered.append((cq_id, text))

        close_calls = []

        async def mock_close_live_position(order_id, reason):
            close_calls.append(order_id)
            return True, "sell-oid"

        class _FakeFrom:
            id = 999  # не admin

        class _FakeCQ:
            id = "cq-3"
            data = "close_pos:order-db-3"
            from_ = _FakeFrom()
            message = None

        def _fake_settings():
            class _S:
                admin_telegram_ids = [1]

            return _S()

        monkeypatch.setattr(tg, "get_settings", _fake_settings)
        monkeypatch.setattr(tg, "answer_callback_query", mock_answer_callback_query)
        monkeypatch.setattr("app.services.live_exit.close_live_position", mock_close_live_position)

        await tg._handle_callback_query(_FakeCQ())

        assert close_calls == [], "non-admin не должен вызвать close_live_position"
        assert answered == [("cq-3", "⛔ Только для админов")]
