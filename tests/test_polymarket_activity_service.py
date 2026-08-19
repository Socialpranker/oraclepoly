"""Тесты PolymarketActivityService."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.polymarket_activity_service import (
    ACTIVITY_API_URL,
    PolymarketActivityService,
    get_polymarket_activity_service,
)


def _async_response(json_data, status: int = 200):
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.json = MagicMock(return_value=json_data)
    response.raise_for_status = MagicMock()
    return response


@pytest.mark.asyncio
async def test_fetch_activity_window_passes_correct_params():
    """Проверяем что start/end/limit/offset идут в запрос."""
    captured = {}

    async def fake_get(self, url, params=None):
        captured["url"] = url
        captured["params"] = params
        return _async_response([])

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        svc = PolymarketActivityService()
        await svc.fetch_activity_window(start=1000, end=2000, limit=100, offset=50)

    assert captured["url"] == ACTIVITY_API_URL
    p = captured["params"]
    assert p["type"] == "TRADE"
    assert p["start"] == 1000
    assert p["end"] == 2000
    assert p["limit"] == 100
    assert p["offset"] == 50


@pytest.mark.asyncio
async def test_fetch_activity_window_returns_data():
    sample = [{"transactionHash": "0xabc", "side": "BUY", "usdcSize": 5000}]

    async def fake_get(self, url, params=None):
        return _async_response(sample)

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        svc = PolymarketActivityService()
        result = await svc.fetch_activity_window(start=1000, end=2000)
    assert result == sample


@pytest.mark.asyncio
async def test_fetch_activity_handles_http_error():
    """При HTTP error вернуть пустой список (graceful degradation)."""
    async def fake_get(self, url, params=None):
        raise httpx.HTTPError("boom")

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        svc = PolymarketActivityService()
        result = await svc.fetch_activity_window(start=1, end=2)
    assert result == []


@pytest.mark.asyncio
async def test_iterate_window_paginates_until_short_page():
    """Если последняя страница неполная — выходим."""
    pages = [[{"i": i} for i in range(500)], [{"i": i} for i in range(500, 700)]]
    call_count = {"n": 0}

    async def fake_fetch(*args, **kwargs):
        idx = call_count["n"]
        call_count["n"] += 1
        return pages[idx] if idx < len(pages) else []

    svc = PolymarketActivityService()
    with patch.object(svc, "fetch_activity_window", side_effect=fake_fetch):
        result = await svc.iterate_window(start=1, end=2, page_size=500)
    assert len(result) == 700
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_iterate_window_respects_max_pages():
    async def fake_fetch(*args, **kwargs):
        return [{"x": 1}] * 500

    svc = PolymarketActivityService()
    with patch.object(svc, "fetch_activity_window", side_effect=fake_fetch):
        result = await svc.iterate_window(start=1, end=2, page_size=500, max_pages=3)
    assert len(result) == 1500


def test_factory_returns_service():
    svc = get_polymarket_activity_service()
    assert isinstance(svc, PolymarketActivityService)
