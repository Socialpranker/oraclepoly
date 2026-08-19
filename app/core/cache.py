"""
Oracle Backend - Redis Cache Service

Simple get/set/delete cache layer between FastAPI and PostgreSQL.
Gracefully degrades: if Redis is unavailable, all operations are no-ops.
"""

import json

import redis.asyncio as redis
import structlog

logger = structlog.get_logger()

_redis: redis.Redis | None = None


async def init_redis(url: str) -> None:
    """Initialize the Redis connection."""
    global _redis
    try:
        # max_connections=200: oracle-recalculate (CONCURRENCY=28 × ~5-8 cache calls/market)
        # даёт пиковую нагрузку ~150-200 параллельных Redis-вызовов. Прежний лимит 50
        # давал 40-220 'Too many connections' ошибок/час → cache miss → REST/DB fallback.
        # Redis server maxclients=10000, запас огромный.
        _redis = redis.from_url(url, decode_responses=True, max_connections=200)
        await _redis.ping()
        logger.info("redis_connected", url=url.split("@")[-1])
    except Exception as e:
        logger.warning("redis_connection_failed", error=str(e))
        _redis = None


async def close_redis() -> None:
    """Close the Redis connection."""
    global _redis
    if _redis:
        await _redis.close()
        _redis = None
        logger.info("redis_closed")


async def get_redis() -> redis.Redis | None:
    """Return the Redis connection (or None if unavailable)."""
    return _redis


async def cache_get(key: str) -> dict | list | None:
    """Get a value from cache. Returns None on miss or if Redis is unavailable."""
    if not _redis:
        return None
    try:
        data = await _redis.get(key)
        return json.loads(data) if data else None
    except Exception as e:
        logger.warning("cache_get_error", key=key, error=str(e))
        return None


async def cache_set(key: str, value, ttl_seconds: int = 300) -> None:
    """Set a value in cache with TTL. No-op if Redis is unavailable."""
    if not _redis:
        return
    try:
        await _redis.set(key, json.dumps(value, default=str), ex=ttl_seconds)
    except Exception as e:
        logger.warning("cache_set_error", key=key, error=str(e))


async def cache_delete_pattern(pattern: str) -> int:
    """Delete all keys matching a pattern. Returns count of deleted keys."""
    if not _redis:
        return 0
    try:
        keys = [key async for key in _redis.scan_iter(match=pattern)]
        if keys:
            deleted = await _redis.delete(*keys)
            logger.info("cache_invalidated", pattern=pattern, count=deleted)
            return deleted
        return 0
    except Exception as e:
        logger.warning("cache_delete_error", pattern=pattern, error=str(e))
        return 0
