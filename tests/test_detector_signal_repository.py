"""Тесты DetectorSignalRepository."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.detectors.contract import Signal
from app.repositories.detector_signal_repository import DetectorSignalRepository


def _signal(**overrides) -> Signal:
    base = {
        "detector": "D1_fade_whale",
        "market_id": "mkt-1",
        "side": "NO",
        "generated_at": datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
        "confidence": 0.7,
        "expected_edge_pct": 80.4,
        "fair_price": 0.4,
        "current_price": 0.45,
        "horizon_sec": 1800,
        "evidence": {"tx_hash": "0xabc", "zone": "mid"},
    }
    base.update(overrides)
    return Signal(**base)


@pytest.mark.asyncio
async def test_insert_returns_true_on_new_row():
    with patch(
        "app.repositories.detector_signal_repository.execute",
        new_callable=AsyncMock,
        return_value="INSERT 0 1",
    ) as mock_execute:
        ok = await DetectorSignalRepository().insert(_signal())
    assert ok is True
    mock_execute.assert_called_once()


@pytest.mark.asyncio
async def test_insert_returns_false_on_conflict():
    with patch(
        "app.repositories.detector_signal_repository.execute",
        new_callable=AsyncMock,
        return_value="INSERT 0 0",
    ):
        ok = await DetectorSignalRepository().insert(_signal())
    assert ok is False


@pytest.mark.asyncio
async def test_insert_serializes_evidence_as_json():
    captured: dict = {}

    async def fake_execute(*args, **kwargs):
        captured["args"] = args
        return "INSERT 0 1"

    with patch(
        "app.repositories.detector_signal_repository.execute",
        side_effect=fake_execute,
    ):
        await DetectorSignalRepository().insert(
            _signal(evidence={"tx_hash": "0xdead", "zone": "high"})
        )

    args = captured["args"]
    assert "ON CONFLICT DO NOTHING" in args[0]
    evidence_arg = args[11]
    assert isinstance(evidence_arg, str)
    assert "0xdead" in evidence_arg


@pytest.mark.asyncio
async def test_stats_by_detector_returns_dicts():
    rows = [
        {"detector": "D1_fade_whale", "n_total": 5, "n_resolved": 3, "avg_confidence": 0.7, "avg_roi_pct": 75.0},
    ]
    with patch(
        "app.repositories.detector_signal_repository.fetch",
        new_callable=AsyncMock,
        return_value=rows,
    ):
        result = await DetectorSignalRepository().stats_by_detector(hours=24)
    assert result == [
        {"detector": "D1_fade_whale", "n_total": 5, "n_resolved": 3, "avg_confidence": 0.7, "avg_roi_pct": 75.0}
    ]
