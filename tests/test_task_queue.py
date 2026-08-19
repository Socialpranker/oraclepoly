"""
Тесты для Redis Task Queue (app.core.task_queue).

Покрытие:
- enqueue/dequeue roundtrip
- debounce_acquire (первый True, второй False)
- graceful degradation без Redis
- queue_length
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_enqueue_dequeue_roundtrip():
    """Задача проходит через LPUSH → BRPOP и десериализуется обратно."""
    queue = []

    mock_redis = AsyncMock()
    mock_redis.llen = AsyncMock(return_value=0)
    mock_redis.lpush = AsyncMock(side_effect=lambda k, v: queue.insert(0, (k, v)))

    async def mock_brpop(key, timeout=5):
        if queue:
            return queue.pop()
        return None

    mock_redis.brpop = AsyncMock(side_effect=mock_brpop)

    with patch("app.core.task_queue._get_redis", return_value=mock_redis):
        from app.core.task_queue import dequeue, enqueue

        ok = await enqueue("test:queue", {"market_id": "abc-123", "amount": 50000})
        assert ok is True

        result = await dequeue("test:queue", timeout=1)
        assert result is not None
        assert result["market_id"] == "abc-123"
        assert result["amount"] == 50000


@pytest.mark.asyncio
async def test_debounce_acquire_first_true_second_false():
    """Первый вызов debounce_acquire → True, повторный → False."""
    locks = {}

    async def mock_set(key, value, ex=None, nx=False):
        if nx and key in locks:
            return False
        locks[key] = value
        return True

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=mock_set)

    with patch("app.core.task_queue._get_redis", return_value=mock_redis):
        from app.core.task_queue import debounce_acquire

        first = await debounce_acquire("oracle:debounce:recalc:market-1", ttl=60)
        assert first is True

        second = await debounce_acquire("oracle:debounce:recalc:market-1", ttl=60)
        assert second is False


@pytest.mark.asyncio
async def test_enqueue_without_redis():
    """Без Redis — enqueue возвращает False, не падает."""
    with patch("app.core.task_queue._get_redis", return_value=None):
        from app.core.task_queue import enqueue

        result = await enqueue("test:queue", {"data": "test"})
        assert result is False


@pytest.mark.asyncio
async def test_dequeue_without_redis():
    """Без Redis — dequeue возвращает None."""
    with patch("app.core.task_queue._get_redis", return_value=None):
        from app.core.task_queue import dequeue

        result = await dequeue("test:queue", timeout=1)
        assert result is None


@pytest.mark.asyncio
async def test_queue_length():
    """queue_length возвращает LLEN."""
    mock_redis = AsyncMock()
    mock_redis.llen = AsyncMock(return_value=42)

    with patch("app.core.task_queue._get_redis", return_value=mock_redis):
        from app.core.task_queue import queue_length

        length = await queue_length("test:queue")
        assert length == 42


@pytest.mark.asyncio
async def test_queue_length_without_redis():
    """Без Redis — queue_length возвращает 0."""
    with patch("app.core.task_queue._get_redis", return_value=None):
        from app.core.task_queue import queue_length

        length = await queue_length("test:queue")
        assert length == 0


@pytest.mark.asyncio
async def test_debounce_without_redis():
    """Без Redis — debounce_acquire возвращает True (пропускаем debounce)."""
    with patch("app.core.task_queue._get_redis", return_value=None):
        from app.core.task_queue import debounce_acquire

        result = await debounce_acquire("oracle:debounce:test", ttl=60)
        assert result is True
