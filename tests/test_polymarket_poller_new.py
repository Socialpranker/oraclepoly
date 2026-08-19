"""Тесты для API-методов PolymarketPollerService (P1).

fetch_open_interest — Gamma API (HTTP).
fetch_market_holders — внутренняя БД (мокаем fetch).
fetch_leaderboard_rank — внутренняя БД (мокаем fetchval).
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.polymarket_poller_service import PolymarketPollerService


@pytest.fixture
def poller():
    return PolymarketPollerService()


class TestFetchOpenInterest:
    """Тесты для fetch_open_interest()."""

    @pytest.mark.asyncio
    async def test_success(self, poller):
        """Возвращает liquidity из Gamma API."""
        mock_response = httpx.Response(
            200,
            json=[{"liquidityNum": 150000.0, "volumeNum": 500000.0, "condition_id": "0xabc"}],
            request=httpx.Request("GET", "https://gamma-api.polymarket.com/markets"),
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            result = await poller.fetch_open_interest("0xabc")
        assert result == {"liquidity": 150000.0, "volume": 500000.0}

    @pytest.mark.asyncio
    async def test_empty_response(self, poller):
        """Пустой ответ API → None."""
        mock_response = httpx.Response(
            200,
            json=[],
            request=httpx.Request("GET", "https://gamma-api.polymarket.com/markets"),
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            result = await poller.fetch_open_interest("0xabc")
        assert result is None

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self, poller):
        """Ошибка API → None (не падает)."""
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.HTTPError("timeout")):
            result = await poller.fetch_open_interest("0xabc")
        assert result is None


class TestFetchMarketHolders:
    """Тесты для fetch_market_holders() — из внутренней БД."""

    @pytest.mark.asyncio
    async def test_success(self, poller):
        """Возвращает топ-холдеров с концентрацией из БД."""
        with patch(
            "app.services.polymarket_poller_service.PolymarketPollerService._fetch_holders_from_db",
            new_callable=AsyncMock,
        ) as mock_db:
            mock_db.return_value = {
                "total_holders": 3,
                "total_size": 100000.0,
                "top5_concentration": 1.0,
                "holders": [
                    {"address": "0xa", "size": 50000.0},
                    {"address": "0xb", "size": 30000.0},
                    {"address": "0xc", "size": 20000.0},
                ],
            }
            result = await poller.fetch_market_holders(condition_id=None, market_id="test-market-id")

        assert result is not None
        assert result["total_holders"] == 3
        assert result["total_size"] == 100000.0
        assert result["top5_concentration"] == 1.0

    @pytest.mark.asyncio
    async def test_no_market_id(self, poller):
        """Нет market_id → None."""
        result = await poller.fetch_market_holders(condition_id=None, market_id=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_error(self, poller):
        """Ошибка БД → None."""
        with patch(
            "app.services.polymarket_poller_service.PolymarketPollerService._fetch_holders_from_db",
            new_callable=AsyncMock,
            side_effect=Exception("DB error"),
        ):
            result = await poller.fetch_market_holders(condition_id=None, market_id="test-id")
        assert result is None


class TestFetchLeaderboardRank:
    """Тесты для fetch_leaderboard_rank() — из внутренней БД."""

    @pytest.mark.asyncio
    async def test_found(self, poller):
        """Кит найден в БД → возвращает rank и метрики."""
        mock_result = '{"rank": 42, "profit": 125000.5, "volume": 2500000.0, "positions": 180, "markets_traded": 95}'
        with patch("app.services.polymarket_poller_service.fetchval", new_callable=AsyncMock, return_value=mock_result):
            result = await poller.fetch_leaderboard_rank("0xwhale")
        assert result is not None
        assert result["rank"] == 42
        assert result["profit"] == 125000.5

    @pytest.mark.asyncio
    async def test_not_found(self, poller):
        """Кит не в БД → None."""
        with patch("app.services.polymarket_poller_service.fetchval", new_callable=AsyncMock, return_value=None):
            result = await poller.fetch_leaderboard_rank("0xnoob")
        assert result is None

    @pytest.mark.asyncio
    async def test_error(self, poller):
        """Ошибка БД → None."""
        with patch(
            "app.services.polymarket_poller_service.fetchval", new_callable=AsyncMock, side_effect=Exception("err")
        ):
            result = await poller.fetch_leaderboard_rank("0xwhale")
        assert result is None
