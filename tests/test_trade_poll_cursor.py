"""Tests for trade_poll timestamp cursor pagination."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTradePoolCursorPagination:
    @pytest.mark.asyncio
    async def test_uses_last_ts_from_redis(self):
        """Если есть last_ts в Redis → фетчит after=last_ts."""
        from app.jobs.trade_poll import run_trade_poll

        mock_trades = [
            {
                "id": "t1",
                "timestamp": 1712000100,
                "size": "500",
                "price": "0.5",
                "asset_id": "token1",
                "side": "BUY",
                "owner": "0xabc",
            },
        ]

        mock_poller = MagicMock()
        mock_poller.fetch_trades_since = AsyncMock(return_value=mock_trades)
        mock_poller.filter_whale_trades = MagicMock(return_value=[])

        with (
            patch("app.jobs.trade_poll.cache_get", AsyncMock(return_value="1712000000")),
            patch("app.jobs.trade_poll.cache_set", AsyncMock()),
            patch("app.jobs.trade_poll.get_poller_service", return_value=mock_poller),
        ):
            result = await run_trade_poll()

        mock_poller.fetch_trades_since.assert_called()
        assert result["fetched"] == 1

    @pytest.mark.asyncio
    async def test_no_last_ts_fetches_without_cursor(self):
        """Нет last_ts в Redis → фетчит без cursor."""
        from app.jobs.trade_poll import run_trade_poll

        mock_poller = MagicMock()
        mock_poller.fetch_trades_since = AsyncMock(return_value=[])

        with (
            patch("app.jobs.trade_poll.cache_get", AsyncMock(return_value=None)),
            patch("app.jobs.trade_poll.cache_set", AsyncMock()),
            patch("app.jobs.trade_poll.get_poller_service", return_value=mock_poller),
        ):
            result = await run_trade_poll()

        assert result["fetched"] == 0
        mock_poller.fetch_trades_since.assert_called_once_with(after_ts=None, limit=500)

    @pytest.mark.asyncio
    async def test_cursor_pagination_multiple_pages(self):
        """Если первая страница полная (500) → фетчит вторую."""
        from app.jobs.trade_poll import run_trade_poll

        page1 = [
            {
                "id": f"t{i}",
                "timestamp": 1712000000 + i,
                "size": "500",
                "price": "0.5",
                "asset_id": "tok",
                "side": "BUY",
                "owner": "0x1",
            }
            for i in range(500)
        ]
        page2 = [
            {
                "id": f"t{i}",
                "timestamp": 1712000500 + i,
                "size": "500",
                "price": "0.5",
                "asset_id": "tok",
                "side": "BUY",
                "owner": "0x1",
            }
            for i in range(100)
        ]

        mock_poller = MagicMock()
        mock_poller.fetch_trades_since = AsyncMock(side_effect=[page1, page2])
        mock_poller.filter_whale_trades = MagicMock(return_value=[])

        with (
            patch("app.jobs.trade_poll.cache_get", AsyncMock(return_value="1712000000")),
            patch("app.jobs.trade_poll.cache_set", AsyncMock()),
            patch("app.jobs.trade_poll.get_poller_service", return_value=mock_poller),
        ):
            result = await run_trade_poll()

        assert result["fetched"] == 600
        assert mock_poller.fetch_trades_since.call_count == 2
