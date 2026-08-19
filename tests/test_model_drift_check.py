"""Тесты model_drift_check job."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.calibration.beta_calibrator import BetaCalibrator
from app.calibration.meta_labeler import (
    MetaLabelerConfig,
    MetaLabelerExample,
    serialize_to_json,
    train_meta_labeler,
)
from app.jobs.model_drift_check import (
    DEGRADATION_RATIO,
    SEVERE_RATIO,
    _check_l1_drift,
    _check_layer_drift,
    run_model_drift_check,
)


def _row(detector="D1_fade_whale", raw_p=0.6, edge=80.0, price=0.4, horizon=1800,
         outcome="WIN", category="Sports", volume=50000.0, generated_at=None):
    return {
        "detector": detector,
        "raw_p": raw_p,
        "expected_edge_pct": edge,
        "entry_price": price,
        "horizon_sec": horizon,
        "generated_at": generated_at or datetime.now(UTC),
        "outcome": outcome,
        "category": category,
        "volume_24h": volume,
    }


def _rows_balanced_winrate(n: int, win_rate: float, seed: int = 42):
    rng = random.Random(seed)
    return [
        _row(
            raw_p=rng.uniform(0.5, 0.7),
            outcome="WIN" if rng.random() < win_rate else "LOSS",
            generated_at=datetime.now(UTC) - timedelta(hours=i),
        )
        for i in range(n)
    ]


def _fitted_calibrator():
    rng = random.Random(0)
    raw_p = [rng.uniform(0.3, 0.8) for _ in range(50)]
    outcome = [1 if rng.random() < 0.6 else 0 for _ in range(50)]
    cal = BetaCalibrator(min_samples=30)
    cal.fit(raw_p, outcome)
    return cal


@pytest.mark.asyncio
async def test_check_l1_drift_returns_none_with_too_few_samples():
    cal = _fitted_calibrator()
    rows = _rows_balanced_winrate(n=10, win_rate=0.6)
    result = await _check_l1_drift("D1_fade_whale", cal, baseline_brier=0.20, rows=rows)
    assert result is None


@pytest.mark.asyncio
async def test_check_l1_drift_severity_ok_when_close():
    cal = _fitted_calibrator()
    rows = _rows_balanced_winrate(n=100, win_rate=0.6, seed=42)
    result = await _check_l1_drift("D1_fade_whale", cal, baseline_brier=0.30, rows=rows)
    assert result is not None
    assert result["severity"] == "ok"


@pytest.mark.asyncio
async def test_check_l1_drift_severity_severe_when_brier_explodes():
    cal = _fitted_calibrator()
    rows = _rows_balanced_winrate(n=100, win_rate=0.6, seed=42)
    # baseline_brier очень маленький → ratio высокий
    result = await _check_l1_drift("D1_fade_whale", cal, baseline_brier=0.05, rows=rows)
    assert result is not None
    assert result["ratio"] >= SEVERE_RATIO
    assert result["severity"] == "severe"


@pytest.mark.asyncio
async def test_check_l1_drift_severity_degraded():
    cal = _fitted_calibrator()
    rows = _rows_balanced_winrate(n=100, win_rate=0.6, seed=42)
    # baseline немного меньше rolling
    result = await _check_l1_drift("D1_fade_whale", cal, baseline_brier=0.20, rows=rows)
    assert result is not None
    if DEGRADATION_RATIO <= result["ratio"] < SEVERE_RATIO:
        assert result["severity"] == "degraded"
    else:
        assert result["severity"] in ("ok", "severe")


@pytest.mark.asyncio
async def test_run_drift_check_no_recent_signals():
    with patch(
        "app.jobs.model_drift_check.fetch", new_callable=AsyncMock, return_value=[]
    ):
        result = await run_model_drift_check()
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_run_drift_check_full_l1_pipeline():
    """End-to-end: fetch returns rows + cals + L2 absent → возвращает только L1."""
    rows = _rows_balanced_winrate(n=100, win_rate=0.6, seed=42)
    cal = _fitted_calibrator()
    cal_params = json.dumps(cal.serialize())

    fetch_mock = AsyncMock()
    fetch_mock.side_effect = [
        rows,
        [{"detector": "D1_fade_whale", "brier_holdout": 0.25}],
    ]
    # Загрузчик L1-калибраторов теперь fetch'ает через fallback_chain
    chain_fetch_mock = AsyncMock(return_value=[{"detector": "D1_fade_whale", "params": cal_params}])

    with (
        patch("app.jobs.model_drift_check.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch(
            "app.jobs.model_drift_check.fetchrow",
            new_callable=AsyncMock,
            return_value=None,  # нет active L2
        ),
    ):
        result = await run_model_drift_check()

    assert result["status"] == "ok"
    assert isinstance(result["l1"], list)
    assert len(result["l1"]) == 1
    assert result["l1"][0]["detector"] == "D1_fade_whale"
    assert result["l2"] is None


@pytest.mark.asyncio
async def test_run_drift_check_l2_demotes_on_severe():
    """L2 severe → execute UPDATE is_active=false."""
    rows = _rows_balanced_winrate(n=200, win_rate=0.5, seed=42)

    cal = _fitted_calibrator()
    cal_params = json.dumps(cal.serialize())

    # Тренируем небольшую L2 модель
    examples = [
        MetaLabelerExample(
            detector_id="D1_fade_whale",
            calibrated_p=0.5,
            edge_pct=80.0,
            entry_price=0.4,
            horizon_sec=1800,
            category="Sports",
            volume_24h=50000.0,
            is_v2_post=True,
            outcome=1 if i % 2 else 0,
        )
        for i in range(200)
    ]
    model = train_meta_labeler(
        examples, config=MetaLabelerConfig(min_train_samples=100)
    )
    model_payload = json.loads(serialize_to_json(model))

    fetch_mock = AsyncMock()
    fetch_mock.side_effect = [
        rows,
        [{"detector": "D1_fade_whale", "brier_holdout": 0.25}],
    ]
    # Загрузчик L1-калибраторов теперь fetch'ает через fallback_chain
    chain_fetch_mock = AsyncMock(return_value=[{"detector": "D1_fade_whale", "params": cal_params}])

    # fetchrow вызывается 3×: L2 active row, затем Layer A / Layer B (None = нет active).
    fetchrow_mock = AsyncMock(
        side_effect=[
            {
                "version": "lgb_l2_v_test",
                "model_payload": model_payload,
                "holdout_brier": 0.01,  # очень низкий → ratio будет огромный
            },
            None,  # ml_event_selector: нет active
            None,  # ml_market_picker: нет active
        ]
    )

    captured_updates: list = []

    async def fake_execute(*args, **kwargs):
        captured_updates.append(args)
        return "UPDATE 0 1"

    with (
        patch("app.jobs.model_drift_check.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch("app.jobs.model_drift_check.fetchrow", fetchrow_mock),
        patch("app.jobs.model_drift_check.execute", side_effect=fake_execute),
    ):
        result = await run_model_drift_check()

    assert result["status"] == "ok"
    assert result["l2"] is not None
    assert result["l2"]["severity"] == "severe"
    assert any("UPDATE ml_meta_labeler" in c[0] for c in captured_updates)


# --- Layer A/B drift monitoring (piece 1 of ML observability) ---

@pytest.mark.asyncio
async def test_check_layer_drift_no_active_model():
    """Нет активной модели → severity 'no_active', не падает."""
    with patch("app.jobs.model_drift_check.fetchrow", AsyncMock(return_value=None)):
        result = await _check_layer_drift("ml_event_selector", "LayerA")
    assert result["severity"] == "no_active"
    assert result["active"] is False


@pytest.mark.asyncio
async def test_check_layer_drift_insufficient_n():
    """live_n < 30 → severity 'insufficient' (не шумим на малой выборке)."""
    row = {"version": "v1", "holdout_per_event_brier": 0.17,
           "live_holdout_brier": 0.26, "live_n_events": 12, "fit_at": None}
    with patch("app.jobs.model_drift_check.fetchrow", AsyncMock(return_value=row)):
        result = await _check_layer_drift("ml_event_selector", "LayerA")
    assert result["severity"] == "insufficient"


@pytest.mark.asyncio
async def test_check_layer_drift_ok_when_stable():
    """live_brier ≈ baseline → ok."""
    row = {"version": "v1", "holdout_per_event_brier": 0.170,
           "live_holdout_brier": 0.172, "live_n_events": 500, "fit_at": None}
    with patch("app.jobs.model_drift_check.fetchrow", AsyncMock(return_value=row)):
        result = await _check_layer_drift("ml_event_selector", "LayerA")
    assert result["severity"] == "ok"
    assert result["ratio"] < DEGRADATION_RATIO


@pytest.mark.asyncio
async def test_check_layer_drift_severe_real_case():
    """Реальный кейс Layer A: baseline 0.17 → live 0.266 (ratio ~1.56) → severe."""
    row = {"version": "event_selector_v20260514", "holdout_per_event_brier": 0.170,
           "live_holdout_brier": 0.266, "live_n_events": 3431, "fit_at": None}
    with patch("app.jobs.model_drift_check.fetchrow", AsyncMock(return_value=row)):
        result = await _check_layer_drift("ml_event_selector", "LayerA")
    assert result["ratio"] >= SEVERE_RATIO
    assert result["severity"] == "severe"
    assert result["active"] is True
