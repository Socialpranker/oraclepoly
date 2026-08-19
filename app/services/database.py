"""
Database Service — asyncpg connection pool.

Прямой доступ к PostgreSQL через asyncpg.
Инициализация/закрытие через FastAPI lifespan.
"""

from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import asyncpg
import structlog

from app.core.config import get_settings

logger = structlog.get_logger()


def _mask_dsn(dsn: str) -> str:
    """Замаскировать пароль в DSN для безопасного логирования."""
    parsed = urlparse(dsn)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    db = parsed.path.lstrip("/") if parsed.path else ""
    return f"{host}:{port}/{db}"


_pool: asyncpg.Pool | None = None


async def init_db(*, dsn: str | None = None, min_size: int = 5, max_size: int = 40) -> None:
    """Создать пул соединений. Вызывается в FastAPI lifespan.

    dsn=None (по умолчанию) — settings.database_url, как раньше.
    dsn=settings.agent_database_url — read-only роль для Phase 0 audit-скриптов.
    """
    global _pool
    settings = get_settings()
    resolved_dsn = dsn or settings.database_url
    _pool = await asyncpg.create_pool(
        dsn=resolved_dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=30,
    )
    logger.info("database_pool_created", host=_mask_dsn(resolved_dsn), min_size=min_size, max_size=max_size)


async def close_db() -> None:
    """Закрыть пул соединений. Вызывается в FastAPI lifespan."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("database_pool_closed")


def get_pool() -> asyncpg.Pool:
    """Получить пул соединений (DI)."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db() first.")
    return _pool


async def fetch(query: str, *args: Any, lock_timeout_ms: int | None = None) -> list[asyncpg.Record]:
    """SELECT — вернуть список записей.

    lock_timeout_ms: если задан — запрос выполняется на выделенном соединении
    под `SET LOCAL lock_timeout`, чтобы ожидание блокировки таблицы было быстрым
    и контролируемым. Нужен для запросов, конкурирующих с maintenance-локами
    (VACUUM FULL markets берёт ACCESS EXCLUSIVE lock ~2 мин по воскресеньям 04:00;
    без lock_timeout asyncpg висел в фазе PREPARE до command_timeout=30s и падал
    по TimeoutError). При срабатывании PostgreSQL бросает QueryCanceledError —
    caller ловит и скипает тик, а не роняет job.
    """
    pool = get_pool()
    if lock_timeout_ms is None:
        return await pool.fetch(query, *args)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # int() — параметризовать SET нельзя ($1 недопустим), защита от инъекции.
            await conn.execute(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}")
            return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    """SELECT — вернуть одну запись или None."""
    pool = get_pool()
    return await pool.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    """SELECT — вернуть скалярное значение."""
    pool = get_pool()
    return await pool.fetchval(query, *args)


async def execute(query: str, *args: Any, timeout: float | None = None) -> str:
    """INSERT/UPDATE/DELETE — вернуть статус (например 'UPDATE 1').

    timeout переопределяет command_timeout пула (30s) — для долгих maintenance-команд
    вроде VACUUM FULL, которые в 30s не укладываются.
    """
    pool = get_pool()
    return await pool.execute(query, *args, timeout=timeout)


async def executemany(query: str, args: list[tuple]) -> None:
    """Batch INSERT/UPDATE."""
    pool = get_pool()
    await pool.executemany(query, args)


@asynccontextmanager
async def transaction():
    """Context manager для выполнения нескольких запросов в одной транзакции.

    Usage:
        async with transaction() as conn:
            await conn.execute("UPDATE ...", ...)
            await conn.fetchrow("UPDATE ...", ...)
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn


def _serialize_value(val: Any) -> Any:
    """Конвертировать asyncpg-типы в JSON-совместимые."""
    import uuid
    from datetime import date, datetime, timedelta
    from decimal import Decimal

    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, timedelta):
        return val.total_seconds()
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, bytes):
        return val.hex()
    return val


def record_to_dict(record: asyncpg.Record | None) -> dict[str, Any] | None:
    """Конвертировать asyncpg.Record в JSON-совместимый dict."""
    if record is None:
        return None
    return {k: _serialize_value(v) for k, v in dict(record).items()}


def records_to_list(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    """Конвертировать список asyncpg.Record в list[dict]."""
    return [{k: _serialize_value(v) for k, v in dict(r).items()} for r in records]
