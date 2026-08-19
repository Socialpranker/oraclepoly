"""Tests for API authentication dependencies."""

import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.auth import optional_telegram_user, require_telegram_user


def create_init_data(user_data: dict, auth_date: int, bot_token: str) -> str:
    """Helper to create valid Telegram initData for testing."""
    user_json = json.dumps(user_data, separators=(",", ":"))
    params = {
        "user": user_json,
        "auth_date": str(auth_date),
        "query_id": "test_query_id",
    }
    sorted_items = sorted(params.items())
    data_check_string = "\n".join([f"{k}={v}" for k, v in sorted_items])
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    params["hash"] = calculated_hash
    return "&".join([f"{k}={v}" for k, v in params.items()])


BOT_TOKEN = "test_bot_token_12345"


@pytest.fixture
def mock_settings():
    with patch("app.core.telegram.get_settings") as mock:
        mock.return_value.telegram_bot_token = BOT_TOKEN
        yield mock


@pytest.fixture
def valid_init_data():
    user = {"id": 12345678, "username": "test_user", "first_name": "Test"}
    return create_init_data(user, int(time.time()) - 60, BOT_TOKEN)


@pytest.fixture
def app_with_routes():
    """Create a test FastAPI app with both dependency types."""
    app = FastAPI()

    @app.get("/required")
    async def required_route(user: dict = Depends(require_telegram_user)):
        return {"user_id": user.get("id")}

    @app.get("/optional")
    async def optional_route(user: dict | None = Depends(optional_telegram_user)):
        return {"user_id": user.get("id") if user else None}

    return app


class TestRequireTelegramUser:
    def test_missing_header_returns_401(self, mock_settings, app_with_routes):
        client = TestClient(app_with_routes)
        response = client.get("/required")
        assert response.status_code == 401

    def test_invalid_header_returns_403(self, mock_settings, app_with_routes):
        client = TestClient(app_with_routes)
        response = client.get("/required", headers={"x-telegram-init-data": "invalid_data"})
        assert response.status_code == 403

    def test_valid_header_returns_user(self, mock_settings, app_with_routes, valid_init_data):
        client = TestClient(app_with_routes)
        response = client.get("/required", headers={"x-telegram-init-data": valid_init_data})
        assert response.status_code == 200
        assert response.json()["user_id"] == 12345678


class TestOptionalTelegramUser:
    def test_missing_header_returns_none(self, mock_settings, app_with_routes):
        client = TestClient(app_with_routes)
        response = client.get("/optional")
        assert response.status_code == 200
        assert response.json()["user_id"] is None

    def test_invalid_header_returns_403(self, mock_settings, app_with_routes):
        client = TestClient(app_with_routes)
        response = client.get("/optional", headers={"x-telegram-init-data": "invalid_data"})
        assert response.status_code == 403

    def test_valid_header_returns_user(self, mock_settings, app_with_routes, valid_init_data):
        client = TestClient(app_with_routes)
        response = client.get("/optional", headers={"x-telegram-init-data": valid_init_data})
        assert response.status_code == 200
        assert response.json()["user_id"] == 12345678
