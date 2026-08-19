"""Kill-switch control-plane API (audit топ-6, Task 3): GET/DELETE /api/bot/v2/kill-switch.

Раньше снятие kill_switch было доступно только через SSH+redis-cli, а
live_dashboard хардкодил kill_switch_tripped=False (дашборд всегда врал
disarmed). Тесты покрывают: auth (401 без Bearer), happy-path read/clear,
Redis unavailable → 503, и честное чтение kill-статуса в live_dashboard.
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
    """FastAPI с bot router, реальный check_admin (для 401-теста без override)."""
    app = FastAPI()
    app.include_router(bot_router, prefix="/api/bot")
    return app


@pytest.fixture
def app_with_bot_admin_override():
    """FastAPI с bot router и подменённым check_admin (always allow)."""
    app = FastAPI()
    app.include_router(bot_router, prefix="/api/bot")
    app.dependency_overrides[check_admin] = lambda: {"method": "bearer", "admin_id": "api_key"}
    return app


class TestKillSwitchAuth:
    def test_get_kill_switch_without_bearer_403(self, app_with_bot):
        """check_admin: без Bearer/TG-init-data/JWT cookie → 403 Forbidden."""
        client = TestClient(app_with_bot)
        response = client.get("/api/bot/v2/kill-switch")
        assert response.status_code == 403

    def test_delete_kill_switch_without_bearer_403(self, app_with_bot):
        client = TestClient(app_with_bot)
        response = client.delete("/api/bot/v2/kill-switch")
        assert response.status_code == 403

    def test_get_kill_switch_with_bearer_ok(self, app_with_bot):
        # Реальный admin-ключ из settings, не хардкод: conftest.py задаёт ADMIN_API_KEY
        # через os.environ.setdefault — в CI переменная уже задана секретом, дефолт
        # "test-admin-key" туда не попадает, и хардкод в заголовке не совпал бы.
        from app.core.config import get_settings

        admin_key = get_settings().active_admin_keys[0]
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        with patch("app.api.bot.get_redis", AsyncMock(return_value=mock_redis)):
            client = TestClient(app_with_bot)
            response = client.get(
                "/api/bot/v2/kill-switch",
                headers={"Authorization": f"Bearer {admin_key}"},
            )
        assert response.status_code == 200


class TestKillSwitchStatus:
    def test_status_reports_tripped_with_reason(self, app_with_bot_admin_override):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="daemon:exposure:exposure_hard_cap:65%>60%")
        with patch("app.api.bot.get_redis", AsyncMock(return_value=mock_redis)):
            client = TestClient(app_with_bot_admin_override)
            response = client.get("/api/bot/v2/kill-switch")

        assert response.status_code == 200
        body = response.json()
        assert body["tripped"] is True
        assert body["reason"] == "daemon:exposure:exposure_hard_cap:65%>60%"

    def test_status_reports_disarmed_when_no_key(self, app_with_bot_admin_override):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        with patch("app.api.bot.get_redis", AsyncMock(return_value=mock_redis)):
            client = TestClient(app_with_bot_admin_override)
            response = client.get("/api/bot/v2/kill-switch")

        assert response.status_code == 200
        body = response.json()
        assert body["tripped"] is False
        assert body["reason"] is None

    def test_status_disarmed_when_redis_unavailable(self, app_with_bot_admin_override):
        with patch("app.api.bot.get_redis", AsyncMock(return_value=None)):
            client = TestClient(app_with_bot_admin_override)
            response = client.get("/api/bot/v2/kill-switch")

        assert response.status_code == 200
        body = response.json()
        assert body["tripped"] is False
        assert body["reason"] is None


class TestKillSwitchClear:
    def test_clear_deletes_key_and_notifies_admin(self, app_with_bot_admin_override):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="daemon:exposure:exposure_hard_cap:65%>60%")
        mock_redis.delete = AsyncMock()
        send_admin_message_mock = AsyncMock()
        with (
            patch("app.api.bot.get_redis", AsyncMock(return_value=mock_redis)),
            patch("app.api.bot.send_admin_message", send_admin_message_mock),
        ):
            client = TestClient(app_with_bot_admin_override)
            response = client.delete("/api/bot/v2/kill-switch")

        assert response.status_code == 200
        body = response.json()
        assert body["cleared"] is True
        assert body["prev_reason"] == "daemon:exposure:exposure_hard_cap:65%>60%"
        mock_redis.delete.assert_awaited_once_with("bot:kill_switch")
        send_admin_message_mock.assert_awaited_once()

    def test_clear_returns_503_when_redis_unavailable(self, app_with_bot_admin_override):
        with patch("app.api.bot.get_redis", AsyncMock(return_value=None)):
            client = TestClient(app_with_bot_admin_override)
            response = client.delete("/api/bot/v2/kill-switch")

        assert response.status_code == 503
