"""Tests for TakerService — Polymarket Data API taker lookup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.taker_service import TakerService


@pytest.mark.asyncio
async def test_resolve_is_taker_found():
    """tx_hash найден в takerOnly=true ответе → True."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = [
        {"transactionHash": "0xabc123", "side": "BUY", "size": "100"},
        {"transactionHash": "0xdef456", "side": "SELL", "size": "200"},
    ]
    fake_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=fake_response)

    with (
        patch("app.services.taker_service.cache_get", return_value=None),
        patch("app.services.taker_service.cache_set", new_callable=AsyncMock),
    ):
        svc = TakerService(client=mock_client)
        result = await svc.resolve_is_taker("0xwhale", "0xcondition", "0xabc123")

    assert result is True


@pytest.mark.asyncio
async def test_resolve_is_taker_not_found_returns_none():
    """tx_hash не найден в ответе → None (не False!)."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = [
        {"transactionHash": "0xother", "side": "BUY"},
    ]
    fake_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=fake_response)

    with (
        patch("app.services.taker_service.cache_get", return_value=None),
        patch("app.services.taker_service.cache_set", new_callable=AsyncMock),
    ):
        svc = TakerService(client=mock_client)
        result = await svc.resolve_is_taker("0xwhale", "0xcondition", "0xmissing")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_is_taker_cached():
    """Кэш hit → не вызывает API."""
    cached_hashes = ["0xabc123", "0xdef456"]

    with patch("app.services.taker_service.cache_get", return_value=cached_hashes):
        svc = TakerService()
        result = await svc.resolve_is_taker("0xwhale", "0xcondition", "0xabc123")

    assert result is True


@pytest.mark.asyncio
async def test_resolve_is_taker_cached_miss():
    """Кэш hit, но tx_hash не в set → None."""
    cached_hashes = ["0xother"]

    with patch("app.services.taker_service.cache_get", return_value=cached_hashes):
        svc = TakerService()
        result = await svc.resolve_is_taker("0xwhale", "0xcondition", "0xmissing")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_is_taker_api_error_returns_none():
    """API ошибка → None."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("timeout"))

    with patch("app.services.taker_service.cache_get", return_value=None):
        svc = TakerService(client=mock_client)
        result = await svc.resolve_is_taker("0xwhale", "0xcondition", "0xabc")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_is_taker_empty_condition_returns_none():
    """Пустой condition_id → None без API вызова."""
    svc = TakerService()
    result = await svc.resolve_is_taker("0xwhale", "", "0xabc")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_batch_returns_set():
    """resolve_batch возвращает set tx_hashes."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = [
        {"transactionHash": "0xabc"},
        {"transactionHash": "0xdef"},
    ]
    fake_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=fake_response)

    svc = TakerService(client=mock_client)
    result = await svc.resolve_batch("0xwhale", "0xcondition")

    assert result == {"0xabc", "0xdef"}


@pytest.mark.asyncio
async def test_resolve_batch_empty():
    """resolve_batch при ошибке → пустой set."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("err"))

    svc = TakerService(client=mock_client)
    result = await svc.resolve_batch("0xwhale", "0xcondition")

    assert result == set()


@pytest.mark.asyncio
async def test_get_taker_service_singleton():
    """get_taker_service возвращает singleton."""
    # Reset singleton
    import app.services.taker_service as mod
    from app.services.taker_service import get_taker_service

    mod._taker_service = None

    svc1 = get_taker_service()
    svc2 = get_taker_service()
    assert svc1 is svc2

    # Cleanup
    mod._taker_service = None
