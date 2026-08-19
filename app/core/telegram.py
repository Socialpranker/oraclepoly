import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

import structlog

from app.core.config import get_settings

logger = structlog.get_logger()

# TTL for initData validation (24 часа — Mini App может быть открыт долго)
INIT_DATA_TTL_SECONDS = 86400


def validate_telegram_init_data(init_data: str) -> dict | None:
    """
    Validates Telegram Mini App initData using the bot token.
    Returns the user data dict if valid, else None.

    Algorithm:
    1. Parse the query string into key-value pairs.
    2. Extract 'hash' and remove it from pairs.
    3. Sort remaining pairs alphabetically.
    4. Create data-check-string as 'key=value\nkey=value'.
    5. Calculate secret key as HMAC-SHA256(data="WebAppData", key=bot_token).
    6. Calculate hash as HMAC-SHA256(data=data-check-string, key=secret_key).
    7. Compare calculated hash with the provided 'hash'.
    """
    settings = get_settings()
    bot_token = settings.telegram_bot_token

    if not bot_token:
        # In development if no token is provided, we might want to bypass (not recommended for production)
        # return {"id": 12345678, "username": "test_user"}
        return None

    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data:
            return None

        # Validate auth_date freshness (Replay Attack protection)
        auth_date_str = parsed_data.get("auth_date")
        if not auth_date_str:
            return None

        try:
            auth_date = int(auth_date_str)
        except ValueError:
            return None

        now = time.time()
        # Защита от auth_date в будущем (бессрочный replay при утечке bot_token)
        if auth_date > now + 60:
            logger.warning("telegram_init_data_future_date", auth_date=auth_date, now=int(now))
            return None
        if now - auth_date > INIT_DATA_TTL_SECONDS:
            logger.warning("telegram_init_data_expired", auth_date=auth_date, now=int(now))
            return None

        received_hash = parsed_data.pop("hash")
        sorted_items = sorted(parsed_data.items())
        data_check_string = "\n".join([f"{k}={v}" for k, v in sorted_items])

        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()

        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        user_data = json.loads(parsed_data.get("user", "{}"))
        return user_data
    except Exception as e:
        logger.error("telegram_validation_error", error=str(e))
        return None
