"""Tests for Redis cache service."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import cache


@pytest.fixture(autouse=True)
def reset_redis():
    """Reset global _redis before each test."""
    cache._redis = None
    yield
    cache._redis = None


class TestInitRedis:
    @pytest.mark.asyncio
    async def test_init_redis_success(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)

        with patch("app.core.cache.redis.from_url", return_value=mock_client):
            await cache.init_redis("redis://localhost:6379/0")

        assert cache._redis is mock_client
        mock_client.ping.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_init_redis_failure_sets_none(self):
        with patch("app.core.cache.redis.from_url", side_effect=ConnectionError("refused")):
            await cache.init_redis("redis://bad-host:6379/0")

        assert cache._redis is None


class TestCloseRedis:
    @pytest.mark.asyncio
    async def test_close_redis(self):
        mock_client = AsyncMock()
        cache._redis = mock_client

        await cache.close_redis()

        mock_client.close.assert_awaited_once()
        assert cache._redis is None

    @pytest.mark.asyncio
    async def test_close_redis_when_not_connected(self):
        await cache.close_redis()  # Should not raise
        assert cache._redis is None


class TestCacheGet:
    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=None)
        cache._redis = mock_client

        result = await cache.cache_get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_hit_returns_data(self):
        data = {"question": "Will BTC hit 100k?", "price": 0.65}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=json.dumps(data))
        cache._redis = mock_client

        result = await cache.cache_get("markets:detail:123")
        assert result == data

    @pytest.mark.asyncio
    async def test_cache_get_returns_list(self):
        data = [{"id": "1"}, {"id": "2"}]
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=json.dumps(data))
        cache._redis = mock_client

        result = await cache.cache_get("markets:list:all:all")
        assert result == data

    @pytest.mark.asyncio
    async def test_cache_get_no_redis_returns_none(self):
        result = await cache.cache_get("any_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_get_error_returns_none(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=ConnectionError("lost"))
        cache._redis = mock_client

        result = await cache.cache_get("key")
        assert result is None


class TestCacheSet:
    @pytest.mark.asyncio
    async def test_cache_set_stores_data(self):
        mock_client = AsyncMock()
        mock_client.set = AsyncMock()
        cache._redis = mock_client

        await cache.cache_set("markets:detail:123", {"price": 0.5}, ttl_seconds=300)

        mock_client.set.assert_awaited_once_with(
            "markets:detail:123",
            json.dumps({"price": 0.5}),
            ex=300,
        )

    @pytest.mark.asyncio
    async def test_cache_set_no_redis_is_noop(self):
        await cache.cache_set("key", "value")  # Should not raise

    @pytest.mark.asyncio
    async def test_cache_set_error_is_silent(self):
        mock_client = AsyncMock()
        mock_client.set = AsyncMock(side_effect=ConnectionError("lost"))
        cache._redis = mock_client

        await cache.cache_set("key", "value")  # Should not raise


class TestCacheDeletePattern:
    @pytest.mark.asyncio
    async def test_delete_matching_keys(self):
        mock_client = AsyncMock()

        async def mock_scan_iter(match=None):
            for key in ["markets:list:all:all", "markets:detail:123"]:
                yield key

        mock_client.scan_iter = mock_scan_iter
        mock_client.delete = AsyncMock(return_value=2)
        cache._redis = mock_client

        deleted = await cache.cache_delete_pattern("markets:*")
        assert deleted == 2
        mock_client.delete.assert_awaited_once_with("markets:list:all:all", "markets:detail:123")

    @pytest.mark.asyncio
    async def test_delete_no_matching_keys(self):
        mock_client = AsyncMock()

        async def mock_scan_iter(match=None):
            return
            yield  # empty async generator

        mock_client.scan_iter = mock_scan_iter
        cache._redis = mock_client

        deleted = await cache.cache_delete_pattern("nonexistent:*")
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_delete_no_redis_returns_zero(self):
        deleted = await cache.cache_delete_pattern("any:*")
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_delete_error_returns_zero(self):
        mock_client = AsyncMock()
        mock_client.scan_iter = MagicMock(side_effect=ConnectionError("lost"))
        cache._redis = mock_client

        deleted = await cache.cache_delete_pattern("key:*")
        assert deleted == 0


class TestGracefulDegradation:
    """Verify all operations work when Redis is unavailable."""

    @pytest.mark.asyncio
    async def test_all_ops_work_without_redis(self):
        assert await cache.cache_get("key") is None
        await cache.cache_set("key", "value")  # no-op
        assert await cache.cache_delete_pattern("key:*") == 0
        await cache.close_redis()  # no-op
