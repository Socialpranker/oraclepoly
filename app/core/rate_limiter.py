import structlog
from fastapi import FastAPI, HTTPException
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.cache import _redis

logger = structlog.get_logger()

# Initialize limiter (IP-based, глобальный)
limiter = Limiter(key_func=get_remote_address)

# Lua-скрипт: atomic INCR + EXPIRE (выполняется за один round-trip)
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local current = redis.call('INCR', key)
if current == 1 then
    redis.call('EXPIRE', key, window)
end
return current
"""


def setup_rate_limiting(app: FastAPI):
    """Configures rate limiting middleware for the application."""
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def check_user_rate_limit(telegram_id: int, action: str, max_requests: int, window_seconds: int = 60) -> None:
    """User-level rate limiting по telegram_id через Redis.

    Дополняет IP-based slowapi лимиты. Atomic INCR+EXPIRE через Lua-скрипт.
    Бросает 429 если лимит превышен. Graceful degradation: если Redis недоступен — пропускает.

    Args:
        telegram_id: Telegram user ID
        action: Название действия (vote, unlock, checkout)
        max_requests: Максимум запросов за окно
        window_seconds: Длительность окна в секундах
    """
    if not _redis:
        return

    key = f"user_rl:{action}:{telegram_id}"
    try:
        current = await _redis.eval(  # noqa: S307 — safe: hardcoded Lua script, not user input
            _RATE_LIMIT_LUA, 1, key, window_seconds
        )

        if current > max_requests:
            logger.warning("user_rate_limit_exceeded", telegram_id=telegram_id, action=action, count=current)
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests. Limit: {max_requests} per {window_seconds}s.",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("user_rate_limit_error", error=str(e))
        # Graceful degradation — пропускаем
