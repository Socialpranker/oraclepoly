"""Тесты Telegram callback_query моделей и хелперов."""

from app.api.telegram import CallbackQuery, TelegramUpdate, TelegramUser  # noqa: F401


def test_callback_query_model_parses():
    """CallbackQuery парсится из dict."""
    data = {
        "id": "123456",
        "from": {"id": 999, "is_bot": False, "first_name": "Test"},
        "data": "promote:fast_no_bias",
    }
    cq = CallbackQuery(**data)
    assert cq.id == "123456"
    assert cq.from_.id == 999
    assert cq.data == "promote:fast_no_bias"


def test_telegram_update_with_callback():
    """TelegramUpdate содержит callback_query."""
    data = {
        "update_id": 1,
        "callback_query": {
            "id": "123",
            "from": {"id": 999, "is_bot": False, "first_name": "Admin"},
            "data": "reject:sniper",
        },
    }
    update = TelegramUpdate(**data)
    assert update.callback_query is not None
    assert update.callback_query.data == "reject:sniper"
    assert update.message is None


def test_telegram_update_without_callback():
    """TelegramUpdate без callback_query (обычное сообщение)."""
    data = {
        "update_id": 2,
        "message": {
            "message_id": 1,
            "chat": {"id": 123, "type": "private"},
            "date": 1000000,
            "text": "/start",
        },
    }
    update = TelegramUpdate(**data)
    assert update.callback_query is None
    assert update.message is not None
