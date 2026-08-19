"""ManifoldClient + manifold_scrape — unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.manifold_service import (
    DEFAULT_PAGE_SIZE,
    ManifoldClient,
    get_manifold_client,
)


def test_factory_returns_client():
    assert isinstance(get_manifold_client(), ManifoldClient)


def test_default_base_url():
    client = ManifoldClient()
    assert client.base_url == "https://api.manifold.markets/v0"


@pytest.mark.asyncio
async def test_list_markets_returns_list():
    fake_data = [
        {"id": "m1", "slug": "test-1", "question": "Q?", "outcomeType": "BINARY"},
        {"id": "m2", "slug": "test-2", "question": "Q2?", "outcomeType": "BINARY"},
    ]
    mock_resp = AsyncMock()
    mock_resp.json = lambda: fake_data
    mock_resp.raise_for_status = lambda: None
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        client = ManifoldClient()
        result = await client.list_markets(limit=2)
    assert len(result) == 2
    assert result[0]["id"] == "m1"


@pytest.mark.asyncio
async def test_list_markets_non_list_response_returns_empty():
    """Manifold gone weird → empty list, no exception."""
    mock_resp = AsyncMock()
    mock_resp.json = lambda: {"error": "rate limited"}
    mock_resp.raise_for_status = lambda: None

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        client = ManifoldClient()
        result = await client.list_markets(limit=10)
    assert result == []


@pytest.mark.asyncio
async def test_list_markets_pagination_cursor():
    """before=<id> passes as query param."""
    fake = [{"id": "m1", "outcomeType": "BINARY"}]
    captured = {}

    async def fake_get(self, url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params", {})
        resp = AsyncMock()
        resp.json = lambda: fake
        resp.raise_for_status = lambda: None
        return resp

    with patch("httpx.AsyncClient.get", new=fake_get):
        client = ManifoldClient()
        await client.list_markets(limit=500, before="prev_market_id")
    assert captured["params"]["limit"] == 500
    assert captured["params"]["before"] == "prev_market_id"


@pytest.mark.asyncio
async def test_get_market_returns_dict():
    fake_market = {"id": "abc", "slug": "x", "question": "?"}
    mock_resp = AsyncMock()
    mock_resp.json = lambda: fake_market
    mock_resp.raise_for_status = lambda: None
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        client = ManifoldClient()
        result = await client.get_market("abc")
    assert result == fake_market


@pytest.mark.asyncio
async def test_get_market_404_returns_none():
    mock_resp = AsyncMock()
    mock_resp.status_code = 404

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        client = ManifoldClient()
        result = await client.get_market("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_get_by_slug():
    fake_market = {"id": "abc", "slug": "test-slug"}
    mock_resp = AsyncMock()
    mock_resp.json = lambda: fake_market
    mock_resp.raise_for_status = lambda: None
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        client = ManifoldClient()
        result = await client.get_by_slug("test-slug")
    assert result == fake_market


@pytest.mark.asyncio
async def test_get_by_slug_404_returns_none():
    mock_resp = AsyncMock()
    mock_resp.status_code = 404

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        client = ManifoldClient()
        result = await client.get_by_slug("missing")
    assert result is None


@pytest.mark.asyncio
async def test_list_active_binary_filters_non_binary():
    """MULTIPLE_CHOICE / PSEUDO_NUMERIC — отсекаем."""
    page = [
        {"id": "m1", "outcomeType": "BINARY", "isResolved": False, "volume24Hours": 100},
        {"id": "m2", "outcomeType": "MULTIPLE_CHOICE", "isResolved": False, "volume24Hours": 100},
        {"id": "m3", "outcomeType": "PSEUDO_NUMERIC", "isResolved": False, "volume24Hours": 100},
    ]
    with patch.object(ManifoldClient, "list_markets", new=AsyncMock(return_value=page)):
        client = ManifoldClient()
        result = await client.list_active_binary_markets(max_pages=1, min_volume_24h=0)
    assert len(result) == 1
    assert result[0]["id"] == "m1"


@pytest.mark.asyncio
async def test_list_active_binary_filters_resolved():
    page = [
        {"id": "m1", "outcomeType": "BINARY", "isResolved": False, "volume24Hours": 100},
        {"id": "m2", "outcomeType": "BINARY", "isResolved": True, "volume24Hours": 100},
    ]
    with patch.object(ManifoldClient, "list_markets", new=AsyncMock(return_value=page)):
        client = ManifoldClient()
        result = await client.list_active_binary_markets(max_pages=1)
    assert len(result) == 1
    assert result[0]["id"] == "m1"


@pytest.mark.asyncio
async def test_list_active_binary_filters_low_volume():
    page = [
        {"id": "m1", "outcomeType": "BINARY", "isResolved": False, "volume24Hours": 100},
        {"id": "m2", "outcomeType": "BINARY", "isResolved": False, "volume24Hours": 10},  # below min
    ]
    with patch.object(ManifoldClient, "list_markets", new=AsyncMock(return_value=page)):
        client = ManifoldClient()
        result = await client.list_active_binary_markets(max_pages=1, min_volume_24h=50)
    assert len(result) == 1
    assert result[0]["id"] == "m1"


@pytest.mark.asyncio
async def test_list_active_binary_paginates():
    """multiple pages combined."""
    page_size_full = [
        {"id": f"m{i}", "outcomeType": "BINARY", "isResolved": False, "volume24Hours": 100}
        for i in range(DEFAULT_PAGE_SIZE)
    ]
    page2_partial = [
        {"id": "m_extra", "outcomeType": "BINARY", "isResolved": False, "volume24Hours": 100}
    ]
    call_count = {"n": 0}

    async def fake_list(self, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return page_size_full
        if call_count["n"] == 2:
            return page2_partial
        return []

    with patch.object(ManifoldClient, "list_markets", new=fake_list):
        client = ManifoldClient()
        result = await client.list_active_binary_markets(max_pages=5, min_volume_24h=0)
    assert len(result) == DEFAULT_PAGE_SIZE + 1
    assert call_count["n"] == 2  # 2nd page partial → loop breaks


@pytest.mark.asyncio
async def test_list_active_binary_max_pages_safety():
    """Если каждая страница полная — выходим после max_pages."""
    full_page = [
        {"id": f"m{i}", "outcomeType": "BINARY", "isResolved": False, "volume24Hours": 100}
        for i in range(DEFAULT_PAGE_SIZE)
    ]
    call_count = {"n": 0}

    async def fake_list(self, **kwargs):
        call_count["n"] += 1
        return full_page

    with patch.object(ManifoldClient, "list_markets", new=fake_list):
        client = ManifoldClient()
        await client.list_active_binary_markets(max_pages=3, min_volume_24h=0)
    assert call_count["n"] == 3  # respected max_pages cap
