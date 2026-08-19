"""Stage 5 / Shadow Validation infrastructure tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_migration_present():
    assert (REPO_ROOT / "migrations/20260501080003_create_pipeline_v2_shadow_predictions.sql").exists()


def test_migration_has_audit_columns():
    sql = (REPO_ROOT / "migrations/20260501080003_create_pipeline_v2_shadow_predictions.sql").read_text()
    assert "monolith_meta_p" in sql
    assert "v2_meta_p" in sql
    assert "v2_p_event" in sql
    assert "v2_passed_gate" in sql
    assert "monolith_version" in sql
    assert "layer_a_version" in sql
    assert "layer_b_version" in sql
    # Idempotency
    assert "UNIQUE (detector_signal_id)" in sql


def test_jobs_importable():
    from app.jobs import pipeline_v2_comparison_report, pipeline_v2_outcome_sync, pipeline_v2_shadow_predictor

    assert asyncio.iscoroutinefunction(pipeline_v2_shadow_predictor.run_pipeline_v2_shadow_predictor)
    assert asyncio.iscoroutinefunction(pipeline_v2_outcome_sync.run_pipeline_v2_outcome_sync)
    assert asyncio.iscoroutinefunction(pipeline_v2_comparison_report.run_pipeline_v2_comparison_report)


def test_cron_registered():
    from scripts.cron_runner import COMMANDS, JOB_DESCRIPTIONS

    for task in (
        "pipeline-v2-shadow-predictor",
        "pipeline-v2-outcome-sync",
        "pipeline-v2-comparison-report",
    ):
        assert task in COMMANDS
        assert task in JOB_DESCRIPTIONS


def test_crontab_has_stage_5_entries():
    crontab = (REPO_ROOT / "crontab.conf").read_text()
    assert "pipeline-v2-shadow-predictor" in crontab
    assert "*/10 * * * *" in crontab  # every 10 min
    assert "pipeline-v2-outcome-sync" in crontab
    assert "pipeline-v2-comparison-report" in crontab


# ---- Shadow predictor logic ----

def test_shadow_predictor_no_common_signals():
    """Если нет общих signals между mono и v2 — ничего не вставляется."""
    from app.jobs.pipeline_v2_shadow_predictor import run_pipeline_v2_shadow_predictor

    with (
        patch(
            "app.services.detector_signal_service.fetch_calibrated_detector_signals",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.two_stage_inference.fetch_two_stage_calibrated_signals",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.two_stage_inference.fetch_ev_regressor_signals",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = asyncio.run(run_pipeline_v2_shadow_predictor())

    assert result["status"] == "ok"
    assert result["n_inserted"] == 0
    assert result["n_seen_common"] == 0


def test_shadow_predictor_records_both_paths():
    """Common signal присутствует в обоих pathах → INSERT с обоими meta_p."""
    from app.jobs.pipeline_v2_shadow_predictor import run_pipeline_v2_shadow_predictor

    mono_sig = {
        "detector_signal_id": "abc-123",
        "detector": "D1_fade_whale",
        "market_id": "m1",
        "event_id": "ev1",
        "side": "NO",
        "calibrated_p": 0.65,
        "meta_p": 0.62,
    }
    v2_sig = {
        "detector_signal_id": "abc-123",
        "detector": "D1_fade_whale",
        "market_id": "m1",
        "event_id": "ev1",
        "side": "NO",
        "calibrated_p": 0.65,
        "meta_p": 0.70,
        "p_event": 0.75,
        "passed_layer_a_gate": True,
    }

    captured: list = []

    async def execute_mock(query, *args):
        captured.append(args)
        return "INSERT 0 1"

    async def fetch_mock(query, *args):
        return [{"n": 0}]  # zero pre-existing

    async def fetchrow_mock(query, *args):
        return {"version": "test_v1"}  # synthetic active versions

    with (
        patch(
            "app.services.detector_signal_service.fetch_calibrated_detector_signals",
            new=AsyncMock(return_value=[mono_sig]),
        ),
        patch(
            "app.services.two_stage_inference.fetch_two_stage_calibrated_signals",
            new=AsyncMock(return_value=[v2_sig]),
        ),
        patch(
            "app.services.two_stage_inference.fetch_ev_regressor_signals",
            new=AsyncMock(return_value=[]),  # EV-regressor not fit yet (cold-start)
        ),
        patch("app.jobs.pipeline_v2_shadow_predictor.execute", new=AsyncMock(side_effect=execute_mock)),
        patch("app.jobs.pipeline_v2_shadow_predictor.fetch", new=AsyncMock(side_effect=fetch_mock)),
        patch("app.jobs.pipeline_v2_shadow_predictor.fetchrow", new=AsyncMock(side_effect=fetchrow_mock)),
    ):
        result = asyncio.run(run_pipeline_v2_shadow_predictor())

    assert result["status"] == "ok"
    assert result["n_seen_common"] == 1
    # INSERT был сделан
    assert len(captured) == 1
    args = captured[0]
    # args[5]=monolith_calibrated_p, args[6]=monolith_meta_p,
    # args[7]=v2_calibrated_p, args[10]=v2_meta_p
    assert args[6] == 0.62  # mono meta_p
    assert args[10] == 0.70  # v2 meta_p
    assert args[8] == 0.75  # v2 p_event
    assert args[9] is True  # passed gate


# ---- Outcome sync ----

def test_outcome_sync_returns_counters():
    from app.jobs.pipeline_v2_outcome_sync import run_pipeline_v2_outcome_sync

    pending_states = [42, 35]  # before, after

    async def fetchval_mock(query, *args):
        return pending_states.pop(0)

    with (
        patch("app.jobs.pipeline_v2_outcome_sync.fetchval", new=AsyncMock(side_effect=fetchval_mock)),
        patch("app.jobs.pipeline_v2_outcome_sync.execute", new=AsyncMock(return_value="UPDATE 7")),
    ):
        result = asyncio.run(run_pipeline_v2_outcome_sync())

    assert result["status"] == "ok"
    assert result["synced"] == 7  # 42 - 35
    assert result["still_pending"] == 35


# ---- Comparison report ----

def test_comparison_report_no_data():
    from app.jobs.pipeline_v2_comparison_report import run_pipeline_v2_comparison_report

    with patch("app.jobs.pipeline_v2_comparison_report.fetch", new=AsyncMock(return_value=[])):
        result = asyncio.run(run_pipeline_v2_comparison_report())
    assert result["status"] == "no_data"


def test_comparison_report_evaluates_gates():
    """End-to-end report. v2 better → all gates pass (с n_events ≥ 50)."""
    from datetime import UTC, datetime

    from app.jobs.pipeline_v2_comparison_report import run_pipeline_v2_comparison_report

    pre_v2 = datetime(2026, 4, 30, tzinfo=UTC)
    rows = []
    # 60 events × 5 markets = 300 rows. mono=0.5 (random), v2=0.7 (better) where outcome=1
    import numpy as np

    rng = np.random.default_rng(42)
    for i in range(300):
        outcome = int(rng.integers(0, 2))
        rows.append({
            "detector_signal_id": f"s{i}",
            "detector": "D1_fade_whale",
            "market_id": f"m{i}",
            "event_id": f"ev{i // 5}",
            "side": "NO",
            "monolith_meta_p": 0.5,
            "v2_meta_p": 0.7 if outcome == 1 else 0.3,  # v2 более точная
            "v2_passed_gate": True,
            "outcome_int": outcome,
            "predicted_at": pre_v2,
            "event_size": 5,
            "category": "Politics",
        })

    with patch("app.jobs.pipeline_v2_comparison_report.fetch", new=AsyncMock(return_value=rows)):
        result = asyncio.run(run_pipeline_v2_comparison_report())

    assert result["status"] == "ok"
    assert result["n_rows"] == 300
    assert result["n_events"] == 60
    # v2 brier должен быть лучше mono
    assert result["v2_brier"] < result["mono_brier"]
    assert result["gates"]["brier_v2_better_than_mono"] is True
    assert result["gates"]["n_events_ge_50"] is True  # 60 ≥ 50
    assert result["gates"]["all_passed"] is True


def test_comparison_report_n_events_below_50_blocks():
    from datetime import UTC, datetime

    from app.jobs.pipeline_v2_comparison_report import run_pipeline_v2_comparison_report

    pre_v2 = datetime(2026, 4, 30, tzinfo=UTC)
    rows = [
        {
            "detector_signal_id": f"s{i}",
            "detector": "D1_fade_whale",
            "market_id": f"m{i}",
            "event_id": f"ev{i // 5}",
            "side": "NO",
            "monolith_meta_p": 0.5,
            "v2_meta_p": 0.5,
            "v2_passed_gate": True,
            "outcome_int": 1,
            "predicted_at": pre_v2,
            "event_size": 5,
            "category": "Politics",
        }
        for i in range(50)  # 10 events
    ]

    with patch("app.jobs.pipeline_v2_comparison_report.fetch", new=AsyncMock(return_value=rows)):
        result = asyncio.run(run_pipeline_v2_comparison_report())

    assert result["gates"]["n_events_ge_50"] is False
    assert result["gates"]["all_passed"] is False
