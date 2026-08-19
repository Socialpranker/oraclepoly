"""
Верификация Telegram Login Widget и JWT cookie helpers.

Telegram Login Widget использует другой алгоритм чем Mini App initData:
- secret_key = SHA256(bot_token)  — не HMAC("WebAppData", bot_token)
- Допустимый TTL: 86400 сек (24ч) для виджета
"""

import hashlib
import hmac
import time

import jwt
import structlog

logger = structlog.get_logger()

WIDGET_TTL_SECONDS = 86400  # 24ч
JWT_TTL_SECONDS = 30 * 86400  # 30 дней
JWT_ALGORITHM = "HS256"


def verify_telegram_widget_data(
    widget_data: dict[str, str],
    bot_token: str,
) -> dict | None:
    """
    Верифицирует данные от Telegram Login Widget.

    Returns:
        dict с user info (id как int) или None если невалидно
    """
    if not bot_token:
        logger.warning("web_auth_no_bot_token")
        return None

    try:
        data = dict(widget_data)
        received_hash = data.pop("hash", None)
        if not received_hash:
            return None

        auth_date_str = data.get("auth_date")
        if not auth_date_str:
            return None

        try:
            auth_date = int(auth_date_str)
        except ValueError:
            return None

        now = time.time()
        if auth_date > now + 60:
            logger.warning("widget_auth_future_date", auth_date=auth_date)
            return None
        if now - auth_date > WIDGET_TTL_SECONDS:
            logger.warning("widget_auth_expired", auth_date=auth_date)
            return None

        # Telegram считает hash только по полям, которые он реально прислал в виджете.
        # У пользователя без @username или без фото эти поля отсутствуют — но наш фронт
        # подставляет '' и шлёт. Нужно исключить пустые до хеширования, иначе HMAC расходится.
        filtered = {k: v for k, v in data.items() if v not in (None, "")}

        secret_key = hashlib.sha256(bot_token.encode()).digest()
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(filtered.items()))
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            logger.warning("widget_auth_hash_mismatch")
            return None

        user_id = data.get("id")
        if not user_id:
            return None

        return {
            "id": int(user_id),
            "first_name": data.get("first_name", ""),
            "username": data.get("username", ""),
            "photo_url": data.get("photo_url", ""),
        }

    except Exception as e:
        logger.error("widget_auth_error", error=str(e))
        return None


def make_jwt_cookie(telegram_id: int, secret: str) -> str:
    """Создаёт подписанный JWT токен с telegram_id. Срок хранения JWT_TTL_SECONDS."""
    payload = {
        "sub": str(telegram_id),
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_TTL_SECONDS,
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_jwt_cookie(token: str, secret: str) -> int | None:
    """Декодирует JWT токен. Возвращает telegram_id или None если невалиден/просрочен."""
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except Exception:
        return None
