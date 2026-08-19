"""Тесты ml_meta_labeler_fit cron."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.calibration.beta_calibrator import BetaCalibrator
from app.jobs.ml_meta_labeler_fit import (
    MIN_TRAIN_SAMPLES,
    _row_to_example,
    run_ml_meta_labeler_fit,
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


def _synth_rows(n: int, seed: int = 42):
    rng = random.Random(seed)
    base_time = datetime.now(UTC) - timedelta(days=30)
    rows = []
    for i in range(n):
        rows.append(_row(
            detector=rng.choice(["D1_fade_whale", "D3_cross_mkt_arb"]),
            raw_p=rng.uniform(0.3, 0.9),
            edge=rng.uniform(20, 100),
            price=rng.uniform(0.2, 0.8),
            outcome="WIN" if rng.random() < 0.6 else "LOSS",
            category=rng.choice(["Sports", "Politics", "Crypto"]),
            generated_at=base_time + timedelta(hours=i),
        ))
    return rows


def test_row_to_example_uses_calibrator_when_available():
    cals = {}
    cal = BetaCalibrator(min_samples=10)
    rng = random.Random(0)
    raw_p = [rng.uniform(0.3, 0.8) for _ in range(50)]
    outcome = [1 if rng.random() < 0.5 else 0 for _ in range(50)]
    cal.fit(raw_p, outcome)
    cals["D1_fade_whale"] = cal

    row = _row(detector="D1_fade_whale", raw_p=0.7)
    ex = _row_to_example(row, cals)
    assert ex.calibrated_p != 0.7  # должен примениться калибратор
    assert 0.0 <= ex.calibrated_p <= 1.0


def test_row_to_example_falls_back_to_raw_when_no_calibrator():
    row = _row(detector="D1_fade_whale", raw_p=0.7)
    ex = _row_to_example(row, cals={})
    assert ex.calibrated_p == 0.7


def test_row_to_example_handles_v2_post_flag():
    pre_v2 = _row(generated_at=datetime(2026, 4, 1, tzinfo=UTC))
    post_v2 = _row(generated_at=datetime(2026, 4, 30, tzinfo=UTC))
    assert _row_to_example(pre_v2, {}).is_v2_post is False
    assert _row_to_example(post_v2, {}).is_v2_post is True


def test_row_to_example_outcome_mapping():
    win = _row(outcome="WIN")
    loss = _row(outcome="LOSS")
    assert _row_to_example(win, {}).outcome == 1
    assert _row_to_example(loss, {}).outcome == 0


@pytest.mark.asyncio
async def test_no_data_returns_skipped():
    with patch("app.jobs.ml_meta_labeler_fit.fetch", new_callable=AsyncMock, return_value=[]):
        result = await run_ml_meta_labeler_fit()
    assert result["status"] == "skipped"
    assert result["reason"] == "no_data"


@pytest.mark.asyncio
async def test_too_few_samples_returns_skipped():
    rows = _synth_rows(n=MIN_TRAIN_SAMPLES - 10)
    with (
        patch("app.jobs.ml_meta_labeler_fit.fetch", new_callable=AsyncMock) as mock_fetch,
        patch("app.jobs.ml_meta_labeler_fit.execute", new_callable=AsyncMock),
    ):
        # Первый fetch — training data, второй — calibrators (пустой)
        mock_fetch.side_effect = [rows, []]
        result = await run_ml_meta_labeler_fit()
    assert result["status"] == "skipped"
    assert result["reason"] == "too_few_samples"


@pytest.mark.asyncio
async def test_full_fit_produces_active_model_first_time():
    """Первая запись должна стать active (active_brier=None)."""
    rows = _synth_rows(n=200)

    captured_writes: list = []

    async def fake_execute(*args, **kwargs):
        captured_writes.append(args)
        return "INSERT 0 1"

    fetch_mock = AsyncMock()
    fetch_mock.side_effect = [rows, []]  # training rows, no calibrators

    with (
        patch("app.jobs.ml_meta_labeler_fit.fetch", fetch_mock),
        patch("app.jobs.ml_meta_labeler_fit.execute", side_effect=fake_execute),
        patch(
            "app.jobs.ml_meta_labeler_fit.fetchrow",
            new_callable=AsyncMock,
            return_value=None,  # нет active модели
        ),
    ):
        result = await run_ml_meta_labeler_fit()

    assert result["status"] == "ok"
    assert result["promoted"] is True
    assert result["n_train"] > 0
    assert result["n_holdout"] > 0
    assert "version" in result
    assert "feature_importance" in result

    # Проверяем что было: INSERT новой версии + UPDATE для promotion
    sql_statements = [call[0] for call in captured_writes]
    assert any("INSERT INTO ml_meta_labeler" in q for q in sql_statements)
    assert any("UPDATE ml_meta_labeler" in q for q in sql_statements)

    insert_calls = [call for call in captured_writes if "INSERT INTO ml_meta_labeler" in call[0]]
    assert len(insert_calls) == 1
    payload_json = insert_calls[0][2]
    payload = json.loads(payload_json)
    assert "model_str" in payload
    assert "feature_names" in payload


@pytest.mark.asyncio
async def test_no_promotion_when_new_model_worse():
    """active_brier=0.10, new=0.15 → не promote."""
    rows = _synth_rows(n=200)

    captured_writes: list = []

    async def fake_execute(*args, **kwargs):
        captured_writes.append(args[0])
        return "INSERT 0 1"

    fetch_mock = AsyncMock()
    fetch_mock.side_effect = [rows, []]

    with (
        patch("app.jobs.ml_meta_labeler_fit.fetch", fetch_mock),
        patch("app.jobs.ml_meta_labeler_fit.execute", side_effect=fake_execute),
        patch(
            "app.jobs.ml_meta_labeler_fit.fetchrow",
            new_callable=AsyncMock,
            return_value={"holdout_brier": 0.05},  # очень хороший active
        ),
    ):
        result = await run_ml_meta_labeler_fit()

    assert result["promoted"] is False
    # INSERT прошёл, promote-UPDATE (is_active = true) — нет.
    assert any("INSERT INTO ml_meta_labeler" in q for q in captured_writes)
    assert not any("is_active = true" in q for q in captured_writes)
    # НО last_skip_reason записан (наблюдаемость frozen-by-gate).
    assert any("last_skip_reason" in q for q in captured_writes)
