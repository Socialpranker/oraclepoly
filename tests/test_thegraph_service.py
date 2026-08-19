"""Tests for The Graph resolution detection service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTheGraphService:
    def _make_service(self):
        """Создать TheGraphService с мок-ключом."""
        from app.services.thegraph_service import TheGraphService

        with patch("app.services.thegraph_service.get_settings") as mock_settings:
            mock_settings.return_value.thegraph_api_key = "test-key"
            return TheGraphService()

    @pytest.mark.asyncio
    async def test_check_redemption_returns_true_when_redeemed(self):
        service = self._make_service()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"redemptions": [{"payer": "0xabc", "collateralToken": "0xUSDC", "indexSets": ["1"]}]}
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await service.check_market_resolved("0xcondition123")

        assert result is True

    @pytest.mark.asyncio
    async def test_check_redemption_returns_false_when_not_redeemed(self):
        service = self._make_service()

        mock_response = MagicMock()
        mock_response.json.return_value = {"data": {"redemptions": []}}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await service.check_market_resolved("0xcondition123")

        assert result is False

    @pytest.mark.asyncio
    async def test_api_error_returns_false(self):
        service = self._make_service()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("timeout"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await service.check_market_resolved("0xcondition123")

        assert result is False

    @pytest.mark.asyncio
    async def test_transient_network_error_retried_not_swallowed(self):
        """Сетевая ошибка — не «рынок не резолвнут»: transient ConnectError
        ретраится (http_retry), и второй успешный ответ даёт True. До фикса
        первый же ConnectError молча возвращал False (ложный not-resolved,
        который уровень 4 каскада ловил с задержкой)."""
        import httpx

        service = self._make_service()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"redemptions": [{"payer": "0xabc", "collateralToken": "0xUSDC", "indexSets": ["1"]}]}
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=[httpx.ConnectError("boom"), mock_response])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await service.check_market_resolved("0xcondition123")

        assert result is True
        assert mock_client.post.await_count == 2

    @pytest.mark.asyncio
    async def test_skips_when_no_api_key(self):
        from app.services.thegraph_service import TheGraphService

        with patch("app.services.thegraph_service.get_settings") as mock_settings:
            mock_settings.return_value.thegraph_api_key = ""
            service = TheGraphService()
            result = await service.check_market_resolved("0xcondition123")

        assert result is False
