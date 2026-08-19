"""
Oracle Backend — Redis Task Queue

Очередь задач поверх существующего Redis из cache.py.
Graceful degradation: при недоступности Redis enqueue логирует warning и возвращает False.
"""

import json

import structlog

logger = structlog.get_logger()


def _get_redis():
    """Получить текущее Redis-соединение из cache.py."""
    from app.core.cache import _redis

    return _redis


MAX_QUEUE_LENGTH = 10000


async def enqueue(queue_name: str, data: dict) -> bool:
    """Добавить задачу в очередь (LPUSH). Отклоняет если очередь переполнена."""
    redis = _get_redis()
    if not redis:
        logger.warning("task_queue_no_redis", queue=queue_name)
        return False
    try:
        length = await redis.llen(queue_name)
        if length >= MAX_QUEUE_LENGTH:
            logger.warning("task_queue_full", queue=queue_name, length=length)
            return False
        await redis.lpush(queue_name, json.dumps(data, default=str))
        return True
    except Exception as e:
        logger.warning("task_queue_enqueue_error", queue=queue_name, error=str(e))
        return False


async def dequeue(queue_name: str, timeout: int = 5) -> dict | None:
    """Забрать задачу из очереди (BRPOP с таймаутом)."""
    redis = _get_redis()
    if not redis:
        return None
    try:
        result = await redis.brpop(queue_name, timeout=timeout)
        if result:
            _, raw = result
            return json.loads(raw)
        return None
    except Exception as e:
        logger.warning("task_queue_dequeue_error", queue=queue_name, error=str(e))
        return None


async def debounce_acquire(key: str, ttl: int) -> bool:
    """Попытаться захватить debounce-лок (SET NX EX)."""
    redis = _get_redis()
    if not redis:
        return True  # без Redis — пропускаем debounce
    try:
        acquired = await redis.set(key, "1", ex=ttl, nx=True)
        return bool(acquired)
    except Exception as e:
        logger.warning("task_queue_debounce_error", key=key, error=str(e))
        return True  # при ошибке — пропускаем debounce


async def queue_length(queue_name: str) -> int:
    """Длина очереди (LLEN)."""
    redis = _get_redis()
    if not redis:
        return 0
    try:
        return await redis.llen(queue_name)
    except Exception as e:
        logger.warning("task_queue_length_error", queue=queue_name, error=str(e))
        return 0
