"""
Tests for Telegram authentication security.
Covers replay attack protection via auth_date validation.
"""

import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest

from app.core.telegram import INIT_DATA_TTL_SECONDS, validate_telegram_init_data


def create_init_data(user_data: dict, auth_date: int, bot_token: str) -> str:
    """Helper to create valid Telegram initData for testing."""
    user_json = json.dumps(user_data, separators=(",", ":"))

    params = {
        "user": user_json,
        "auth_date": str(auth_date),
        "query_id": "test_query_id",
    }

    # Sort and create data-check-string
    sorted_items = sorted(params.items())
    data_check_string = "\n".join([f"{k}={v}" for k, v in sorted_items])

    # Calculate hash
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()

    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    # Build query string
    params["hash"] = calculated_hash
    return "&".join([f"{k}={v}" for k, v in params.items()])


class TestReplayAttackProtection:
    """Tests for auth_date validation (replay attack protection)."""

    @pytest.fixture
    def mock_settings(self):
        """Mock settings with test bot token."""
        with patch("app.core.telegram.get_settings") as mock:
            mock.return_value.telegram_bot_token = "test_bot_token_12345"
            yield mock

    def test_expired_init_data_rejected(self, mock_settings):
        """Expired initData (older than TTL) should be rejected."""
        user_data = {"id": 12345678, "username": "test_user"}
        old_auth_date = int(time.time()) - INIT_DATA_TTL_SECONDS - 100

        init_data = create_init_data(user_data=user_data, auth_date=old_auth_date, bot_token="test_bot_token_12345")

        result = validate_telegram_init_data(init_data)
        assert result is None, "Expired initData should be rejected"

    def test_fresh_init_data_accepted(self, mock_settings):
        """Fresh initData (within TTL) should be accepted."""
        user_data = {"id": 12345678, "username": "test_user"}
        fresh_auth_date = int(time.time()) - 60  # 1 minute ago

        init_data = create_init_data(user_data=user_data, auth_date=fresh_auth_date, bot_token="test_bot_token_12345")

        result = validate_telegram_init_data(init_data)
        assert result is not None, "Fresh initData should be accepted"
        assert result.get("id") == 12345678

    def test_missing_auth_date_rejected(self, mock_settings):
        """initData without auth_date should be rejected."""
        # Manually create init_data without auth_date
        user_data = {"id": 12345678, "username": "test_user"}
        user_json = json.dumps(user_data, separators=(",", ":"))

        params = {"user": user_json}

        # Calculate hash without auth_date
        sorted_items = sorted(params.items())
        data_check_string = "\n".join([f"{k}={v}" for k, v in sorted_items])

        secret_key = hmac.new(b"WebAppData", b"test_bot_token_12345", hashlib.sha256).digest()

        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        params["hash"] = calculated_hash
        init_data = "&".join([f"{k}={v}" for k, v in params.items()])

        result = validate_telegram_init_data(init_data)
        assert result is None, "initData without auth_date should be rejected"

    def test_invalid_auth_date_rejected(self, mock_settings):
        """initData with invalid auth_date format should be rejected."""
        user_data = {"id": 12345678, "username": "test_user"}
        user_json = json.dumps(user_data, separators=(",", ":"))

        params = {
            "user": user_json,
            "auth_date": "not_a_number",  # Invalid
        }

        sorted_items = sorted(params.items())
        data_check_string = "\n".join([f"{k}={v}" for k, v in sorted_items])

        secret_key = hmac.new(b"WebAppData", b"test_bot_token_12345", hashlib.sha256).digest()

        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        params["hash"] = calculated_hash
        init_data = "&".join([f"{k}={v}" for k, v in params.items()])

        result = validate_telegram_init_data(init_data)
        assert result is None, "initData with invalid auth_date should be rejected"

    def test_boundary_ttl_accepted(self, mock_settings):
        """initData at exactly TTL boundary should still be accepted."""
        user_data = {"id": 12345678, "username": "test_user"}
        # Exactly at boundary (minus 1 second for safety)
        boundary_auth_date = int(time.time()) - INIT_DATA_TTL_SECONDS + 1

        init_data = create_init_data(
            user_data=user_data, auth_date=boundary_auth_date, bot_token="test_bot_token_12345"
        )

        result = validate_telegram_init_data(init_data)
        assert result is not None, "initData at TTL boundary should be accepted"


class TestHashValidation:
    """Tests for HMAC hash validation."""

    @pytest.fixture
    def mock_settings(self):
        """Mock settings with test bot token."""
        with patch("app.core.telegram.get_settings") as mock:
            mock.return_value.telegram_bot_token = "test_bot_token_12345"
            yield mock

    def test_invalid_hash_rejected(self, mock_settings):
        """initData with tampered hash should be rejected."""
        user_data = {"id": 12345678, "username": "test_user"}
        auth_date = int(time.time()) - 60

        init_data = create_init_data(user_data=user_data, auth_date=auth_date, bot_token="test_bot_token_12345")

        # Tamper with the hash
        init_data = init_data.replace("hash=", "hash=tampered")

        result = validate_telegram_init_data(init_data)
        assert result is None, "Tampered hash should be rejected"

    def test_missing_hash_rejected(self, mock_settings):
        """initData without hash should be rejected."""
        init_data = "user=%7B%22id%22%3A12345%7D&auth_date=123456789"

        result = validate_telegram_init_data(init_data)
        assert result is None, "Missing hash should be rejected"

    def test_no_bot_token_rejected(self):
        """initData should be rejected if no bot token is configured."""
        with patch("app.core.telegram.get_settings") as mock:
            mock.return_value.telegram_bot_token = ""

            init_data = "user=%7B%22id%22%3A12345%7D&auth_date=123456789&hash=abc"
            result = validate_telegram_init_data(init_data)
            assert result is None, "Should reject when no bot token configured"
