"""
Retry Logic with Tenacity

Provides retry decorators for transient failures with exponential backoff.
"""

import asyncio
import logging
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# Polymarket hosts которые мы считаем для health-monitoring. Совпадение по suffix.
# При добавлении нового Polymarket endpoint — добавить сюда.
_POLYMARKET_HOST_SUFFIXES = (
    "polymarket.com",
    "polymarket.io",
)


def _record_polymarket_error(status: int, host: str) -> None:
    """Best-effort Redis counter increment для polymarket_api_health monitor.

    Fail-silent: если Redis недоступен, не ломаем retry-path. Counter
    хранит N ошибок за текущий час bucket'ом по статусу:
      polymarket:api:errors:{YYYYMMDDHH}:{status}
    TTL 25h — следующий часовой job их прочитает и удалит.
    """

    async def _bump():
        try:
            from app.core.cache import get_redis

            redis = await get_redis()
            if not redis:
                return
            bucket = datetime.now(UTC).strftime("%Y%m%d%H")
            key = f"polymarket:api:errors:{bucket}:{status}"
            await redis.incr(key)
            await redis.expire(key, 25 * 3600)
        except Exception:
            pass

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_bump())
    except RuntimeError:
        # No running event loop (sync context) — skip silently.
        # Polymarket calls всегда async, sync path означает что caller
        # — это test или dev tool, метрики не нужны.
        pass


def _is_polymarket_host(url: str) -> bool:
    """True если URL ведёт к Polymarket API."""
    try:
        host = urlparse(url).hostname or ""
        return any(host.endswith(suffix) for suffix in _POLYMARKET_HOST_SUFFIXES)
    except Exception:
        return False


def _is_retryable_http_error(exc: BaseException) -> bool:
    """Retry только на transient ошибки: 5xx, 429. Не retry на 4xx (400, 402, 403, 404).

    Side-effect: при retryable Polymarket ошибке инкрементим Redis counter
    для polymarket_api_health monitor (см. app/jobs/polymarket_api_health.py).
    """
    if isinstance(exc, httpx.ConnectTimeout | httpx.ReadTimeout | httpx.ConnectError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429 or status >= 500:
            url = str(exc.response.url)
            if _is_polymarket_host(url):
                _record_polymarket_error(status, urlparse(url).hostname or "")
            return True
    return False


# =============================================================================
# HTTP Retry Decorator
# =============================================================================

# 2026-05-13: Downgraded retry log level WARNING → INFO. Polymarket /trades
# rate-limits на 429 expected (we throttle to concurrency=2), retry самовосстанавливается.
# Раньше 16K WARNING/час в live-wallet-flow-writer.log замусоривали мониторинг,
# скрывая real errors.
http_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retryable_http_error),
    before_sleep=before_sleep_log(logger, logging.INFO),
    reraise=True,
)
"""
Retry decorator for HTTP operations.

- Retries up to 3 times
- Exponential backoff: 2s -> 4s -> 8s (max 10s)
- Retries on: timeouts, connection errors, HTTP errors

Example:
    @http_retry
    async def fetch_data():
        async with httpx.AsyncClient() as client:
            response = await client.get("https://api.example.com")
            response.raise_for_status()
            return response.json()
"""


# =============================================================================
# OpenAI/LLM Retry Decorator
# =============================================================================

openai_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    retry=retry_if_exception_type(Exception),  # OpenAI SDK exceptions
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
"""
Retry decorator for OpenAI API calls.

- Retries up to 3 times
- Longer exponential backoff for rate limits: 4s -> 8s -> 16s (max 60s)
- Retries on any exception (covers various OpenAI error types)

Example:
    @openai_retry
    async def generate_completion(prompt: str):
        response = await openai.ChatCompletion.acreate(...)
        return response.choices[0].message.content
"""


# =============================================================================
# Database Retry Decorator
# =============================================================================

db_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
"""
Retry decorator for database operations.

- Retries up to 3 times
- Short exponential backoff: 1s -> 2s -> 4s (max 5s)
- Retries on any exception

Example:
    @db_retry
    async def insert_record(data: dict):
        await execute("INSERT INTO records (...) VALUES ($1, $2)", ...)
"""


# =============================================================================
# Light Retry (for less critical operations)
# =============================================================================

light_retry = retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, min=1, max=3),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(logger, logging.DEBUG),
    reraise=True,
)
"""
Light retry decorator for non-critical operations.

- Retries only once (2 attempts total)
- Quick backoff: 1s -> 2s (max 3s)
- Logs at DEBUG level

Example:
    @light_retry
    async def send_notification():
        ...
"""
