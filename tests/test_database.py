"""Tests for app.services.database — фокус на fetch(lock_timeout_ms=...).

Контекст: v2-execution fetch (JOIN markets) падал по TimeoutError раз в неделю,
когда параллельно шёл VACUUM FULL markets (ACCESS EXCLUSIVE lock ~2 мин, вс 04:00).
asyncpg висел в фазе PREPARE на ожидании AccessShareLock до command_timeout=30s.
lock_timeout_ms делает ожидание лока быстрым и контролируемым (asyncpg бросает
QueryCanceledError вместо 30s-виса), тик просто скипается без падения job.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import database


def _make_pool_with_conn() -> tuple[MagicMock, AsyncMock]:
    """Пул, чей acquire() отдаёт мок-соединение через async-context-manager,
    и conn.transaction() — тоже async-context-manager. Возвращает (pool, conn)."""
    conn = AsyncMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    @asynccontextmanager
    async def _txn():
        yield None

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=_acquire)
    conn.transaction = MagicMock(side_effect=_txn)
    return pool, conn


@pytest.mark.asyncio
async def test_fetch_default_path_uses_pool_fetch_no_acquire():
    """Без lock_timeout_ms — прежнее поведение: прямой pool.fetch, без acquire/txn."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=["row"])
    with patch.object(database, "get_pool", return_value=pool):
        result = await database.fetch("SELECT 1")
    assert result == ["row"]
    pool.fetch.assert_awaited_once_with("SELECT 1")
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_with_lock_timeout_sets_local_then_fetches():
    """С lock_timeout_ms — acquire соединения, SET LOCAL lock_timeout в транзакции,
    затем conn.fetch тем же запросом/аргументами."""
    pool, conn = _make_pool_with_conn()
    conn.fetch = AsyncMock(return_value=["row"])
    with patch.object(database, "get_pool", return_value=pool):
        result = await database.fetch("SELECT $1", 42, lock_timeout_ms=3000)
    assert result == ["row"]
    pool.acquire.assert_called_once()
    conn.execute.assert_awaited_once_with("SET LOCAL lock_timeout = 3000")
    conn.fetch.assert_awaited_once_with("SELECT $1", 42)


@pytest.mark.asyncio
async def test_fetch_lock_timeout_coerced_to_int():
    """lock_timeout_ms приводится к int (защита от инъекции через f-string)."""
    pool, conn = _make_pool_with_conn()
    conn.fetch = AsyncMock(return_value=[])
    with patch.object(database, "get_pool", return_value=pool):
        await database.fetch("SELECT 1", lock_timeout_ms=1500.9)
    conn.execute.assert_awaited_once_with("SET LOCAL lock_timeout = 1500")
