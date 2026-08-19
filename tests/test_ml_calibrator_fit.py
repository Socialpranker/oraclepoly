"""Тесты ml_calibrator_fit cron."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.ml_calibrator_fit import _split_train_holdout, run_ml_calibrator_fit


def test_split_train_holdout_proportions():
    train_p, train_y, hold_p, hold_y = _split_train_holdout(
        [0.1] * 100, [0] * 100, seed=42
    )
    assert len(train_p) + len(hold_p) == 100
    assert len(hold_p) == 20  # 20% holdout
    assert len(train_p) == 80


@pytest.mark.asyncio
async def test_no_data_returns_empty():
    with patch("app.jobs.ml_calibrator_fit.fetch", new_callable=AsyncMock, return_value=[]):
        result = await run_ml_calibrator_fit()
    assert result == {"detectors": [], "fitted": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_skip_detector_with_too_few_samples():
    rows = [
        {"detector": "D1_fade_whale", "raw_p": 0.7, "outcome": "WIN"} for _ in range(10)
    ]
    with (
        patch("app.jobs.ml_calibrator_fit.fetch", new_callable=AsyncMock, return_value=rows),
        patch("app.jobs.ml_calibrator_fit.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_ml_calibrator_fit()
    assert result["skipped"] == 1
    assert result["fitted"] == 0
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_fit_two_detectors_writes_both():
    rows = (
        [{"detector": "D1_fade_whale", "raw_p": 0.7 if i % 2 else 0.3, "outcome": "WIN" if i % 2 else "LOSS"} for i in range(100)]
        + [{"detector": "D3_cross_mkt_arb", "raw_p": 0.6 if i % 3 else 0.4, "outcome": "WIN" if i % 3 else "LOSS"} for i in range(100)]
    )

    captured_calls: list = []

    async def fake_execute(*args):
        captured_calls.append(args)
        return "INSERT 0 1"

    with (
        patch("app.jobs.ml_calibrator_fit.fetch", new_callable=AsyncMock, return_value=rows),
        patch("app.jobs.ml_calibrator_fit.execute", side_effect=fake_execute),
    ):
        result = await run_ml_calibrator_fit()

    assert result["fitted"] == 2
    assert result["skipped"] == 0
    assert len(captured_calls) == 2
    detectors_persisted = {call[1] for call in captured_calls}
    assert detectors_persisted == {"D1_fade_whale", "D3_cross_mkt_arb"}

    for call in captured_calls:
        params_json = call[2]
        params = json.loads(params_json)
        assert "a" in params
        assert "b" in params
        assert "c" in params
        n_train, n_holdout = call[3], call[4]
        assert n_train > 0
        assert n_holdout > 0


@pytest.mark.asyncio
async def test_summary_contains_metrics_for_each_detector():
    rows = [
        {"detector": "D1_fade_whale", "raw_p": 0.7 if i % 2 else 0.3, "outcome": "WIN" if i % 2 else "LOSS"}
        for i in range(60)
    ]
    with (
        patch("app.jobs.ml_calibrator_fit.fetch", new_callable=AsyncMock, return_value=rows),
        patch("app.jobs.ml_calibrator_fit.execute", new_callable=AsyncMock),
    ):
        result = await run_ml_calibrator_fit()
    detector_summary = result["detectors"][0]
    assert detector_summary["detector"] == "D1_fade_whale"
    assert detector_summary["fitted"] is True
    assert "brier_raw" in detector_summary
    assert "brier_holdout" in detector_summary
