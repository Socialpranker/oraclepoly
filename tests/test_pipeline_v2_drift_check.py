"""Tests для app/jobs/pipeline_v2_drift_check.py.

Stage 5.5 / Two-Stage ML Pipeline.

Drift status calculation: ratio = live_brier / training_brier.
- ratio < 1.30 → ok
- 1.30 ≤ ratio < 1.50 → moderate alert
- ratio ≥ 1.50 → severe alert
"""

from __future__ import annotations

import asyncio
import math
from unittest.mock import AsyncMock, patch

import pytest

# ---- Drift status calculation ----


def test_within_tolerance_no_alert():
    from app.jobs.pipeline_v2_drift_check import _compute_drift_status

    status = _compute_drift_status(training_brier=0.20, live_brier=0.22)
    assert status["alert"] is False
    assert status["severity"] == "ok"
    assert status["ratio"] == pytest.approx(1.10, rel=0.01)


def test_moderate_alert():
    from app.jobs.pipeline_v2_drift_check import _compute_drift_status

    status = _compute_drift_status(training_brier=0.20, live_brier=0.30)
    assert status["alert"] is True
    assert status["severity"] == "moderate"
    assert status["ratio"] == pytest.approx(1.50, rel=0.01) or status["ratio"] >= 1.30


def test_severe_alert():
    from app.jobs.pipeline_v2_drift_check import _compute_drift_status

    status = _compute_drift_status(training_brier=0.20, live_brier=0.31)
    assert status["alert"] is True
    assert status["severity"] == "severe"


def test_zero_training_brier_handled():
    from app.jobs.pipeline_v2_drift_check import _compute_drift_status

    status = _compute_drift_status(training_brier=0.0, live_brier=0.10)
    assert status["alert"] is True
    assert status["ratio"] == math.inf


def test_perfect_training_better_than_live():
    """live_brier < training_brier → ratio < 1, definitely no alert."""
    from app.jobs.pipeline_v2_drift_check import _compute_drift_status

    status = _compute_drift_status(training_brier=0.20, live_brier=0.10)
    assert status["alert"] is False
    assert status["severity"] == "ok"


# ---- Job orchestration ----


def test_no_active_models_returns_skipped():
    from app.jobs.pipeline_v2_drift_check import run_pipeline_v2_drift_check

    with patch("app.jobs.pipeline_v2_drift_check.fetchrow", new=AsyncMock(return_value=None)):
        result = asyncio.run(run_pipeline_v2_drift_check())
    assert result["status"] == "skipped"
    assert result["reason"] == "no_active_layer_models"


def test_no_live_data_returns_skipped():
    from app.jobs.pipeline_v2_drift_check import run_pipeline_v2_drift_check

    layer_a = {"version": "ev1", "holdout_per_event_brier": 0.20}
    layer_b = {"version": "mp1", "holdout_per_event_brier": 0.18}

    rows = []

    async def fetchrow_mock(query, *args):
        if "ml_event_selector" in query:
            return layer_a
        if "ml_market_picker" in query:
            return layer_b
        return None

    async def fetch_mock(query, *args):
        return rows

    with (
        patch("app.jobs.pipeline_v2_drift_check.fetchrow", new=AsyncMock(side_effect=fetchrow_mock)),
        patch("app.jobs.pipeline_v2_drift_check.fetch", new=AsyncMock(side_effect=fetch_mock)),
    ):
        result = asyncio.run(run_pipeline_v2_drift_check())
    assert result["status"] == "skipped"
    assert result["reason"] == "no_live_resolved_data"


def test_too_few_live_events_returns_skipped():
    from app.jobs.pipeline_v2_drift_check import run_pipeline_v2_drift_check

    layer_a = {"version": "ev1", "holdout_per_event_brier": 0.20}
    layer_b = {"version": "mp1", "holdout_per_event_brier": 0.18}

    # 5 events × 2 markets = 10 rows, but n_events < MIN_LIVE_EVENTS=30
    rows = []
    for i in range(10):
        rows.append({
            "event_id": f"ev{i // 2}",
            "v2_p_event": 0.7,
            "v2_meta_p": 0.6,
            "outcome_int": 1 if i % 2 else 0,
        })

    async def fetchrow_mock(query, *args):
        if "ml_event_selector" in query:
            return layer_a
        if "ml_market_picker" in query:
            return layer_b
        return None

    with (
        patch("app.jobs.pipeline_v2_drift_check.fetchrow", new=AsyncMock(side_effect=fetchrow_mock)),
        patch("app.jobs.pipeline_v2_drift_check.fetch", new=AsyncMock(return_value=rows)),
    ):
        result = asyncio.run(run_pipeline_v2_drift_check())
    assert result["status"] == "skipped"
    assert result["reason"] == "too_few_live_events"
    assert result["min_required"] == 30


def test_full_run_returns_layer_a_b_status():
    """End-to-end: 30+ events live → возвращает Layer A и Layer B status dicts."""
    import numpy as np

    from app.jobs.pipeline_v2_drift_check import run_pipeline_v2_drift_check

    layer_a = {"version": "ev1", "holdout_per_event_brier": 0.20}
    layer_b = {"version": "mp1", "holdout_per_event_brier": 0.18}

    rng = np.random.default_rng(42)
    rows = []
    for i in range(150):  # 50 events × 3 markets
        rows.append({
            "event_id": f"ev{i // 3}",
            "v2_p_event": float(rng.uniform(0.4, 0.8)),
            "v2_meta_p": float(rng.uniform(0.3, 0.7)),
            "outcome_int": int(rng.integers(0, 2)),
        })

    async def fetchrow_mock(query, *args):
        if "ml_event_selector" in query:
            return layer_a
        if "ml_market_picker" in query:
            return layer_b
        return None

    with (
        patch("app.jobs.pipeline_v2_drift_check.fetchrow", new=AsyncMock(side_effect=fetchrow_mock)),
        patch("app.jobs.pipeline_v2_drift_check.fetch", new=AsyncMock(return_value=rows)),
    ):
        result = asyncio.run(run_pipeline_v2_drift_check())

    assert result["status"] == "ok"
    assert result["n_live_events"] == 50
    assert "layer_a" in result
    assert "layer_b" in result
    for layer in (result["layer_a"], result["layer_b"]):
        assert "ratio" in layer
        assert "alert" in layer
        assert "severity" in layer
        assert "training_brier" in layer
        assert "live_brier" in layer


def test_constants():
    from app.jobs import pipeline_v2_drift_check as mod

    assert mod.ALERT_RATIO == 1.30
    assert mod.SEVERE_RATIO == 1.50
    assert mod.MIN_LIVE_EVENTS == 30


def test_cron_registered():
    from scripts.cron_runner import COMMANDS, JOB_DESCRIPTIONS

    assert "pipeline-v2-drift-check" in COMMANDS
    assert "pipeline-v2-drift-check" in JOB_DESCRIPTIONS


def test_crontab_entry():
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    crontab = (repo / "crontab.conf").read_text()
    assert "pipeline-v2-drift-check" in crontab
    assert "30 6 * * 0" in crontab  # Sun 06:30 UTC
