"""Межпроцессный rate-limiter для data-api.polymarket.com /trades.

Контекст (nine-point-audit п.4, замер 2026-07-05 16:00 UTC): 4 cron-джобы
(d26-scan, live-wallet-flow, live-trade-bucket, trade-poll) — отдельные
процессы, суммарно ~11.8K req/час к /trades → 460-680 HTTP 429/час и
47 финальных провалов/час у d26 (детектор молча теряет трейды по ~7%
рынков прогона). In-memory semaphore не работает между процессами —
бюджет делится через Redis fixed-window per-second счётчик.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_acquire_passes_when_under_budget():
    """Счётчик секунды ниже бюджета — слот выдаётся без ожидания."""
    from app.core.data_api_limiter import acquire_data_api_slot

    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=1)
    mock_redis.expire = AsyncMock()

    with patch("app.core.cache.get_redis", AsyncMock(return_value=mock_redis)):
        await acquire_data_api_slot()

    mock_redis.incr.assert_awaited_once()
    mock_redis.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_acquire_waits_when_budget_exhausted():
    """Первая секунда переполнена — limiter спит и берёт слот в следующей."""
    from app.core.data_api_limiter import DATA_API_BUDGET_PER_SEC, acquire_data_api_slot

    mock_redis = AsyncMock()
    # 1-й INCR: бюджет пробит; 2-й INCR (после сна): свободно
    mock_redis.incr = AsyncMock(side_effect=[DATA_API_BUDGET_PER_SEC + 1, 1])
    mock_redis.expire = AsyncMock()

    with (
        patch("app.core.cache.get_redis", AsyncMock(return_value=mock_redis)),
        patch("app.core.data_api_limiter.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        await acquire_data_api_slot()

    assert mock_redis.incr.await_count == 2
    mock_sleep.assert_awaited()


@pytest.mark.asyncio
async def test_acquire_fail_open_when_redis_down():
    """Redis недоступен — не блокируем запрос (как остальные Redis-гейты)."""
    from app.core.data_api_limiter import acquire_data_api_slot

    with patch("app.core.cache.get_redis", AsyncMock(return_value=None)):
        await acquire_data_api_slot()  # не должно бросить и не должно зависнуть


@pytest.mark.asyncio
async def test_acquire_fail_open_on_redis_error():
    """Ошибка INCR — fail-open, запрос идёт без лимита."""
    from app.core.data_api_limiter import acquire_data_api_slot

    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(side_effect=RuntimeError("redis down"))

    with patch("app.core.cache.get_redis", AsyncMock(return_value=mock_redis)):
        await acquire_data_api_slot()


@pytest.mark.asyncio
async def test_fetch_trades_once_acquires_slot_before_request():
    """detector_helpers._fetch_trades_once берёт слот ДО HTTP-вызова."""
    import httpx

    from app.services.detector_helpers import _fetch_trades_once

    call_order: list[str] = []

    async def fake_acquire():
        call_order.append("slot")

    mock_response = AsyncMock()
    mock_response.raise_for_status = lambda: call_order.append("http")
    mock_response.json = lambda: []

    mock_client = AsyncMock(spec=httpx.AsyncClient)

    async def fake_get(*args, **kwargs):
        return mock_response

    mock_client.get = fake_get

    with patch("app.services.detector_helpers.acquire_data_api_slot", side_effect=fake_acquire):
        await _fetch_trades_once(mock_client, "0xcond", 500, 10.0)

    assert call_order == ["slot", "http"]


@pytest.mark.asyncio
async def test_trades_pager_acquires_slot_before_request():
    """polymarket_trades_pager._fetch_one_page берёт слот ДО HTTP-вызова."""
    import httpx

    from app.services.polymarket_trades_pager import _fetch_one_page

    call_order: list[str] = []

    async def fake_acquire():
        call_order.append("slot")

    mock_response = AsyncMock()
    mock_response.raise_for_status = lambda: call_order.append("http")
    mock_response.json = lambda: []

    mock_client = AsyncMock(spec=httpx.AsyncClient)

    async def fake_get(*args, **kwargs):
        return mock_response

    mock_client.get = fake_get

    with patch("app.services.polymarket_trades_pager.acquire_data_api_slot", side_effect=fake_acquire):
        await _fetch_one_page(mock_client, {"market": "0xcond", "limit": 500}, 10.0)

    assert call_order == ["slot", "http"]
