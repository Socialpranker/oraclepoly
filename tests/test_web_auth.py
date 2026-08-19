import hashlib
import hmac
import time

from app.core.web_auth import decode_jwt_cookie, make_jwt_cookie, verify_telegram_widget_data

BOT_TOKEN = "test_bot_token_123"


def _make_valid_widget_data(bot_token: str = BOT_TOKEN, offset_seconds: int = 0) -> dict:
    """Генерирует валидные данные Telegram Login Widget для тестов."""
    auth_date = int(time.time()) + offset_seconds
    data = {
        "id": "12345678",
        "first_name": "Test",
        "username": "testuser",
        "auth_date": str(auth_date),
    }
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    hash_val = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    data["hash"] = hash_val
    return data


def test_verify_valid_widget_data():
    result = verify_telegram_widget_data(
        widget_data=_make_valid_widget_data(),
        bot_token=BOT_TOKEN,
    )
    assert result is not None
    assert result["id"] == 12345678
    assert result["username"] == "testuser"


def test_verify_invalid_hash():
    data = _make_valid_widget_data()
    data["hash"] = "deadbeef" * 8
    result = verify_telegram_widget_data(widget_data=data, bot_token=BOT_TOKEN)
    assert result is None


def test_verify_expired_auth_date():
    data = _make_valid_widget_data(offset_seconds=-(86400 + 60))
    result = verify_telegram_widget_data(widget_data=data, bot_token=BOT_TOKEN)
    assert result is None


def test_verify_future_auth_date():
    data = _make_valid_widget_data(offset_seconds=120)
    result = verify_telegram_widget_data(widget_data=data, bot_token=BOT_TOKEN)
    assert result is None


def test_jwt_roundtrip():
    secret = "test_secret_key_32chars_minimum!!"
    token = make_jwt_cookie(telegram_id=12345678, secret=secret)
    assert isinstance(token, str)
    result = decode_jwt_cookie(token=token, secret=secret)
    assert result == 12345678


def test_jwt_invalid_token():
    result = decode_jwt_cookie(token="garbage.token.here", secret="secret")
    assert result is None


def test_verify_empty_bot_token():
    data = _make_valid_widget_data()
    result = verify_telegram_widget_data(widget_data=data, bot_token="")
    assert result is None


def test_verify_missing_hash():
    data = _make_valid_widget_data()
    del data["hash"]
    result = verify_telegram_widget_data(widget_data=data, bot_token=BOT_TOKEN)
    assert result is None


def test_verify_skips_empty_optional_fields():
    """Telegram не присылает пустые username/photo_url. Если фронт всё равно отправляет
    их как '', сервер должен игнорировать пустые значения при подсчёте hash —
    иначе HMAC расходится с тем, что Telegram уже подписал."""
    # Hash считается только по non-empty полям (как делает Telegram)
    auth_date = int(time.time())
    canonical = {
        "id": "123456789",
        "first_name": "User",
        "auth_date": str(auth_date),
    }
    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(canonical.items()))
    hash_val = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    # Фронт шлёт лишние пустые поля
    payload_with_empties = {
        **canonical,
        "username": "",
        "photo_url": "",
        "hash": hash_val,
    }
    result = verify_telegram_widget_data(widget_data=payload_with_empties, bot_token=BOT_TOKEN)
    assert result is not None
    assert result["id"] == 123456789
