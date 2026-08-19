"""Tests for OracleSnapshotRepository."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.repositories.oracle_snapshot_repository import OracleSnapshotRepository


class TestCreateSnapshot:
    @pytest.mark.asyncio
    async def test_create_snapshot(self):
        snapshot_data = {
            "market_id": str(uuid.uuid4()),
            "oracle_score": 0.7234,
            "market_price": 0.5500,
            "sentiment_score": 0.6100,
            "whale_confidence": 0.4500,
            "risk_score": 0.2000,
        }
        returned_row = {**snapshot_data, "id": str(uuid.uuid4())}

        # create() делегирует BaseRepository._create(), который вызывает fetchrow из base.
        with patch(
            "app.repositories.base.fetchrow",
            new_callable=AsyncMock,
            return_value=returned_row,
        ):
            repo = OracleSnapshotRepository()
            result = await repo.create(snapshot_data)

        assert result["oracle_score"] == 0.7234
        assert result["market_price"] == 0.5500


class TestResolveSnapshotsForMarket:
    @pytest.mark.asyncio
    async def test_resolve_snapshots_for_market(self):
        market_id = str(uuid.uuid4())

        # Single UPDATE resolves all pending snapshots in SQL (brier_score + prediction_correct computed in SQL)
        mock_execute = AsyncMock(return_value="UPDATE 2")

        with patch("app.repositories.oracle_snapshot_repository.execute", mock_execute):
            repo = OracleSnapshotRepository()
            count = await repo.resolve_for_market(market_id, outcome="yes", final_price=0.9500)

        assert count == 2

        # Single UPDATE call with outcome_binary=1.0 for "yes"
        assert mock_execute.call_count == 1
        call_args = mock_execute.call_args[0]
        assert call_args[0]  # SQL query
        assert call_args[1] == "yes"  # outcome
        assert call_args[2] == 0.9500  # final_price
        assert call_args[3] == 1.0  # outcome_binary
        assert call_args[5] == market_id  # market_id

    @pytest.mark.asyncio
    async def test_resolve_no_pending_returns_zero(self):
        # execute returns "UPDATE 0" when no pending snapshots exist
        with patch(
            "app.repositories.oracle_snapshot_repository.execute",
            new_callable=AsyncMock,
            return_value="UPDATE 0",
        ):
            repo = OracleSnapshotRepository()
            count = await repo.resolve_for_market(str(uuid.uuid4()), "yes", 0.95)

        assert count == 0


class TestGetSummaryStats:
    @pytest.mark.asyncio
    async def test_get_summary_stats(self):
        # SQL aggregation: COUNT, COUNT FILTER, AVG
        from decimal import Decimal

        avg_brier = Decimal("0.18")
        mock_row = {"total_resolved": 3, "correct": 2, "avg_brier": avg_brier}

        with patch(
            "app.repositories.oracle_snapshot_repository.fetchrow",
            new_callable=AsyncMock,
            return_value=mock_row,
        ):
            repo = OracleSnapshotRepository()
            stats = await repo.get_summary_stats()

        assert stats["total_resolved"] == 3
        assert stats["accuracy"] == round(2 / 3, 4)
        assert stats["avg_brier_score"] == round(0.18, 4)

    @pytest.mark.asyncio
    async def test_get_summary_stats_empty(self):
        from decimal import Decimal

        mock_row = {"total_resolved": 0, "correct": 0, "avg_brier": Decimal("0")}

        with patch(
            "app.repositories.oracle_snapshot_repository.fetchrow",
            new_callable=AsyncMock,
            return_value=mock_row,
        ):
            repo = OracleSnapshotRepository()
            stats = await repo.get_summary_stats()

        assert stats["total_resolved"] == 0
        assert stats["accuracy"] == 0.0
        assert stats["avg_brier_score"] == 0.0


class TestGetCalibrationData:
    @pytest.mark.asyncio
    async def test_calibration_counts_yes_outcomes_not_prediction_correct(self):
        """get_calibration_data (SQL aggregation) считает долю YES-исходов, а не prediction_correct."""
        from decimal import Decimal

        # SQL GROUP BY bin_idx возвращает агрегированные данные
        mock_rows = [
            {"bin_idx": 0, "predicted": Decimal("0.0800"), "actual": Decimal("0.0000"), "count": 1},
            {"bin_idx": 1, "predicted": Decimal("0.1350"), "actual": Decimal("0.5000"), "count": 2},
            {"bin_idx": 8, "predicted": Decimal("0.8500"), "actual": Decimal("1.0000"), "count": 1},
            {"bin_idx": 9, "predicted": Decimal("0.9000"), "actual": Decimal("1.0000"), "count": 1},
        ]

        with patch(
            "app.repositories.oracle_snapshot_repository.fetch",
            new_callable=AsyncMock,
            return_value=mock_rows,
        ):
            repo = OracleSnapshotRepository()
            calibration = await repo.get_calibration_data()

        # Bin 0.0-0.1: actual YES=0.0
        bin_0 = next(b for b in calibration if b["bin"] == "0.0-0.1")
        assert bin_0["actual"] == 0.0, f"Bin 0.0-0.1 should have actual=0.0, got {bin_0['actual']}"

        # Bin 0.1-0.2: actual YES=0.5
        bin_1 = next(b for b in calibration if b["bin"] == "0.1-0.2")
        assert bin_1["actual"] == 0.5, f"Bin 0.1-0.2 should have actual=0.5, got {bin_1['actual']}"

        # Bin 0.8-0.9: actual YES=1.0
        bin_8 = next(b for b in calibration if b["bin"] == "0.8-0.9")
        assert bin_8["actual"] == 1.0

        # Bin 0.9-1.0: actual YES=1.0
        bin_9 = next(b for b in calibration if b["bin"] == "0.9-1.0")
        assert bin_9["actual"] == 1.0
