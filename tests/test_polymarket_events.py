"""Tests for Polymarket Events API integration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.polymarket_service import PolymarketService


@pytest.mark.asyncio
async def test_fetch_active_events_returns_events():
    """fetch_active_events returns parsed event list from Gamma API."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {
            "id": "event-1",
            "title": "US Election 2028",
            "slug": "us-election-2028",
            "description": "Who will win?",
            "volume": 1000000,
            "volume24hr": 50000,
            "liquidity": 200000,
            "startDate": "2028-01-01T00:00:00Z",
            "endDate": "2028-11-05T00:00:00Z",
            "active": True,
            "closed": False,
            "tags": [{"label": "Politics"}],
            "markets": [
                {"conditionId": "mkt-1", "question": "Will Trump win?"},
                {"conditionId": "mkt-2", "question": "Will Biden win?"},
            ],
        }
    ]
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    service = PolymarketService()

    with patch("httpx.AsyncClient", return_value=mock_client):
        events = await service.fetch_active_events(limit=100, offset=0)

    assert len(events) == 1
    assert events[0]["title"] == "US Election 2028"
    assert len(events[0]["markets"]) == 2


@pytest.mark.asyncio
async def test_fetch_active_events_empty_on_failure():
    """fetch_active_events returns empty list on API failure."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("API down"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    service = PolymarketService()

    with patch("httpx.AsyncClient", return_value=mock_client):
        events = await service.fetch_active_events(limit=100, offset=0)

    assert events == []


@pytest.mark.asyncio
async def test_fetch_active_events_passes_pagination_params():
    """fetch_active_events passes offset and limit parameters."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = []
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    service = PolymarketService()

    with patch("httpx.AsyncClient", return_value=mock_client):
        await service.fetch_active_events(limit=100, offset=200)

    mock_client.get.assert_called_once()
    call_kwargs = mock_client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    assert params["offset"] == 200
    assert params["limit"] == 100
