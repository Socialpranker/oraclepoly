"""Tests for resolve_outcomes — 4-уровневый resolution pipeline."""

from unittest.mock import AsyncMock, patch

import pytest

from app.tasks.resolve_outcomes import (
    PRICE_THRESHOLD_NO,
    PRICE_THRESHOLD_YES,
    resolve_single_market,
    run_resolve_outcomes,
)


class TestResolveOutcomesDBBased:
    """Тесты для run_resolve_outcomes — 5-уровневый sweep.

    Порядок fetch вызовов в run_resolve_outcomes:
    1. gamma_resolved (уровень 2)
    2. remaining_pending (уровень 3) — внутри resolve_single_market: whale_rows fetch
    3. price_proxy_markets (уровень 4)
    4. orphaned paper positions (уровень 5)
    5. vote_pending (_resolve_vote_snapshots_from_db)
    """

    @pytest.mark.asyncio
    async def test_resolves_yes_via_price_proxy(self):
        """current_price > 0.95 → outcome = yes (уровень 4 — price proxy)."""
        market_id = "market-yes-1"

        # gamma_resolved=[], remaining_pending=[], price_proxy=[market],
        # whale_rows (resolve_single snap>0), overdue_markets=[], orphaned=[], vote_pending=[]
        price_proxy = [{"id": market_id, "current_price": 0.98, "is_active": False, "question": "Test?"}]

        mock_fetch = AsyncMock()
        mock_fetch.side_effect = [[], [], price_proxy, [], [], [], []]

        mock_resolve = AsyncMock(return_value=5)

        with (
            patch("app.tasks.resolve_outcomes.fetch", mock_fetch),
            patch("app.tasks.resolve_outcomes.execute", new_callable=AsyncMock),
            patch(
                "app.repositories.oracle_snapshot_repository.OracleSnapshotRepository.resolve_for_market",
                mock_resolve,
            ),
            patch("app.core.cache.cache_delete_pattern", new_callable=AsyncMock),
        ):
            result = await run_resolve_outcomes()

        assert result["oracle_resolved"] == 5
        assert result["status"] == "success"
        mock_resolve.assert_called_once_with(market_id, "yes", 0.98)

    @pytest.mark.asyncio
    async def test_resolves_no_via_price_proxy(self):
        """current_price < 0.05 → outcome = no (уровень 4)."""
        market_id = "market-no-1"

        price_proxy = [{"id": market_id, "current_price": 0.02, "is_active": False, "question": "Will X?"}]

        mock_fetch = AsyncMock()
        mock_fetch.side_effect = [[], [], price_proxy, [], [], [], []]

        mock_resolve = AsyncMock(return_value=3)

        with (
            patch("app.tasks.resolve_outcomes.fetch", mock_fetch),
            patch("app.tasks.resolve_outcomes.execute", new_callable=AsyncMock),
            patch(
                "app.repositories.oracle_snapshot_repository.OracleSnapshotRepository.resolve_for_market",
                mock_resolve,
            ),
            patch("app.core.cache.cache_delete_pattern", new_callable=AsyncMock),
        ):
            result = await run_resolve_outcomes()

        assert result["oracle_resolved"] == 3
        mock_resolve.assert_called_once_with(market_id, "no", 0.02)

    @pytest.mark.asyncio
    async def test_no_pending_snapshots_returns_zero(self):
        """Нет pending снимков → oracle_resolved=0."""
        mock_fetch = AsyncMock()
        # gamma=[], remaining=[], price_proxy=[], overdue_markets=[], orphaned=[]
        mock_fetch.side_effect = [[], [], [], [], []]

        with (
            patch("app.tasks.resolve_outcomes.fetch", mock_fetch),
            patch("app.tasks.resolve_outcomes.execute", new_callable=AsyncMock),
        ):
            result = await run_resolve_outcomes()

        assert result["oracle_resolved"] == 0
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_gamma_resolved_markets(self):
        """Рынки помеченные resolved=true через Gamma (уровень 2)."""
        gamma_resolved = [
            {"id": "m1", "current_price": 0.99, "is_active": False, "question": "Q1", "winner": "Yes"},
            {"id": "m2", "current_price": 0.01, "is_active": False, "question": "Q2", "winner": "No"},
        ]

        mock_fetch = AsyncMock()
        # gamma_resolved, whale_rows_m1, whale_rows_m2, remaining_pending=[], price_proxy=[], overdue=[], orphaned=[], vote=[]
        mock_fetch.side_effect = [gamma_resolved, [], [], [], [], [], [], []]

        resolve_counts = {"m1": 2, "m2": 3}
        mock_resolve = AsyncMock(side_effect=lambda mid, outcome, price: resolve_counts.get(str(mid), 0))

        with (
            patch("app.tasks.resolve_outcomes.fetch", mock_fetch),
            patch("app.tasks.resolve_outcomes.execute", new_callable=AsyncMock),
            patch(
                "app.repositories.oracle_snapshot_repository.OracleSnapshotRepository.resolve_for_market",
                mock_resolve,
            ),
            patch("app.core.cache.cache_delete_pattern", new_callable=AsyncMock),
        ):
            result = await run_resolve_outcomes()

        assert result["oracle_resolved"] == 5

    @pytest.mark.asyncio
    async def test_resolve_error_continues_other_markets(self):
        """Ошибка при resolve одного рынка не прерывает остальные."""
        price_proxy = [
            {"id": "ok-market", "current_price": 0.99, "is_active": False, "question": "OK"},
            {"id": "bad-market", "current_price": 0.01, "is_active": False, "question": "Bad"},
        ]

        mock_fetch = AsyncMock()
        # gamma=[], remaining=[], price_proxy, whale_rows_ok, overdue_markets=[], orphaned=[], vote_pending=[]
        mock_fetch.side_effect = [[], [], price_proxy, [], [], [], []]

        async def mock_resolve_side_effect(market_id, outcome, price):
            if str(market_id) == "bad-market":
                raise RuntimeError("DB error")
            return 4

        mock_resolve = AsyncMock(side_effect=mock_resolve_side_effect)

        with (
            patch("app.tasks.resolve_outcomes.fetch", mock_fetch),
            patch("app.tasks.resolve_outcomes.execute", new_callable=AsyncMock),
            patch(
                "app.repositories.oracle_snapshot_repository.OracleSnapshotRepository.resolve_for_market",
                mock_resolve,
            ),
            patch("app.core.cache.cache_delete_pattern", new_callable=AsyncMock),
        ):
            result = await run_resolve_outcomes()

        assert result["oracle_resolved"] == 4

    def test_thresholds_are_correct(self):
        """Проверяем пороговые значения."""
        assert PRICE_THRESHOLD_YES == 0.95
        assert PRICE_THRESHOLD_NO == 0.05

    def test_no_gamma_api_dependency(self):
        """resolve_outcomes НЕ импортирует polymarket_service."""
        from pathlib import Path

        source = Path("app/tasks/resolve_outcomes.py").read_text()
        assert "polymarket_service" not in source
        assert "get_polymarket_service" not in source


class TestResolveSingleMarket:
    @pytest.mark.asyncio
    async def test_resolves_market_yes(self):
        """resolve_single_market корректно resolve'ит YES outcome."""
        mock_resolve = AsyncMock(return_value=3)
        mock_fetch = AsyncMock(return_value=[])

        with (
            patch(
                "app.repositories.oracle_snapshot_repository.OracleSnapshotRepository.resolve_for_market",
                mock_resolve,
            ),
            patch("app.tasks.resolve_outcomes.fetch", mock_fetch),
            patch("app.core.cache.cache_delete_pattern", new_callable=AsyncMock),
        ):
            result = await resolve_single_market("market-1", "yes", 0.99)

        assert result == 3
        mock_resolve.assert_called_once_with("market-1", "yes", 0.99)

    @pytest.mark.asyncio
    async def test_resolves_market_no(self):
        """resolve_single_market корректно resolve'ит NO outcome."""
        mock_resolve = AsyncMock(return_value=2)
        mock_fetch = AsyncMock(return_value=[])

        with (
            patch(
                "app.repositories.oracle_snapshot_repository.OracleSnapshotRepository.resolve_for_market",
                mock_resolve,
            ),
            patch("app.tasks.resolve_outcomes.fetch", mock_fetch),
            patch("app.core.cache.cache_delete_pattern", new_callable=AsyncMock),
        ):
            result = await resolve_single_market("market-2", "no", 0.02)

        assert result == 2

    @pytest.mark.asyncio
    async def test_error_does_not_raise(self):
        """Ошибка при resolve → логирование, не exception."""
        mock_resolve = AsyncMock(side_effect=RuntimeError("DB error"))

        with patch(
            "app.repositories.oracle_snapshot_repository.OracleSnapshotRepository.resolve_for_market",
            mock_resolve,
        ):
            result = await resolve_single_market("bad-market", "yes", 0.99)

        assert result == 0
