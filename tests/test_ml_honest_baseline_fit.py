"""Tests для app/jobs/ml_honest_baseline_fit.py.

Stage 1 / Two-Stage ML Pipeline.

Smoke тесты с mock'ed DB. Полный тест на реальных данных запускается
руками через scripts/run_honest_baseline.py (требует prod БД).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


def test_module_imports():
    """Smoke: модуль импортится без ошибок."""
    from app.jobs.ml_honest_baseline_fit import run_ml_honest_baseline_fit

    assert asyncio.iscoroutinefunction(run_ml_honest_baseline_fit)


def test_v2_cutover_constant_correct():
    """V2 cutover должен быть 2026-04-28 UTC (см. memory/clob_v2_migration_2026-04-29.md)."""
    from app.jobs.ml_honest_baseline_fit import V2_CUTOVER

    assert V2_CUTOVER == datetime(2026, 4, 28, tzinfo=UTC)


def test_constants_match_plan():
    """Sanity check: ROLLING_DAYS, HOLDOUT_FRACTION, MIN_TRAIN_SAMPLES соответствуют плану."""
    from app.jobs import ml_honest_baseline_fit as mod

    assert mod.ROLLING_DAYS == 365
    assert mod.HOLDOUT_FRACTION == 0.20
    assert mod.MIN_TRAIN_SAMPLES == 100


def test_too_few_samples_returns_skipped():
    """При n < MIN_TRAIN_SAMPLES → status='skipped'."""
    from app.jobs.ml_honest_baseline_fit import run_ml_honest_baseline_fit

    fake_rows = [{"detector": "D1", "raw_p": 0.5, "outcome": "WIN"} for _ in range(10)]
    with patch("app.jobs.ml_honest_baseline_fit.fetch", new=AsyncMock(return_value=fake_rows)):
        result = asyncio.run(run_ml_honest_baseline_fit())
    assert result["status"] == "skipped"
    assert result["reason"] == "too_few_samples"
    assert result["n"] == 10


def test_full_run_with_synthetic_data():
    """End-to-end на синтетике: ≥100 rows → возвращает summary с per-event Brier + HF/Live."""
    import numpy as np

    from app.jobs.ml_honest_baseline_fit import run_ml_honest_baseline_fit

    rng = np.random.default_rng(42)
    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    post_v2 = datetime(2026, 4, 29, tzinfo=UTC)

    rows = []
    for i in range(300):
        # 60 events × 5 markets — даёт events для split
        ev_id = f"ev{i // 5}"
        rows.append({
            "detector": "D1_fade_whale",
            "raw_p": float(rng.uniform(0.3, 0.7)),
            "expected_edge_pct": 10.0,
            "entry_price": 0.5,
            "horizon_sec": 1800,
            "generated_at": pre_v2 if i < 200 else post_v2,
            "outcome": "WIN" if rng.integers(0, 2) else "LOSS",
            "event_id": ev_id,
            "event_size": 5,
            "archetype": "single_market",
            "category": "Politics",
            "volume_24h": 10000.0,
        })

    async def fetch_mock(query, *args):
        if "ml_calibrators" in query:
            return []  # no L1 calibrators → calibrated_p = raw_p
        return rows

    with (
        patch("app.jobs.ml_honest_baseline_fit.fetch", new=AsyncMock(side_effect=fetch_mock)),
        patch("app.jobs.ml_honest_baseline_fit.execute", new=AsyncMock(return_value="INSERT 0 1")),
    ):
        result = asyncio.run(run_ml_honest_baseline_fit())

    assert result["status"] == "ok"
    assert "version" in result
    assert result["version"].startswith("lgb_l2_v_honest_baseline_")
    assert result["n_train_rows"] + result["n_holdout_rows"] == 300
    assert result["n_events_holdout"] > 0

    # HF/Live разделение должно работать
    assert result["hf_pre_v2"]["n_rows"] >= 0
    assert result["live_post_v2"]["n_rows"] >= 0
    # Sum равен n_holdout_rows
    assert result["hf_pre_v2"]["n_rows"] + result["live_post_v2"]["n_rows"] == result["n_holdout_rows"]

    assert "honest_holdout_per_event_brier" in result
    assert 0.0 <= result["honest_holdout_per_event_brier"] <= 1.0


def test_per_detector_breakdown_with_mixed_detectors():
    """End-to-end на синтетике с разными детекторами — per_detector содержит все present детекторы."""
    import numpy as np

    from app.jobs.ml_honest_baseline_fit import run_ml_honest_baseline_fit

    rng = np.random.default_rng(42)
    detectors = ["D1_fade_whale", "D4_small_market_fade", "D5_long_horizon_extreme_fade"]
    rows = []
    # 300 rows: 60 events × 5 markets, 3 detectors round-robin
    for i in range(300):
        rows.append({
            "detector": detectors[i % 3],
            "raw_p": float(rng.uniform(0.3, 0.7)),
            "expected_edge_pct": 10.0,
            "entry_price": 0.5,
            "horizon_sec": 1800,
            "generated_at": datetime(2026, 4, 1, tzinfo=UTC),
            "outcome": "WIN" if rng.integers(0, 2) else "LOSS",
            "event_id": f"ev{i // 5}",
            "event_size": 5,
            "archetype": "single_market",
            "category": "Politics",
            "volume_24h": 10000.0,
        })

    async def fetch_mock(query, *args):
        if "ml_calibrators" in query:
            return []
        return rows

    with (
        patch("app.jobs.ml_honest_baseline_fit.fetch", new=AsyncMock(side_effect=fetch_mock)),
        patch("app.jobs.ml_honest_baseline_fit.execute", new=AsyncMock(return_value="INSERT 0 1")),
    ):
        result = asyncio.run(run_ml_honest_baseline_fit())

    assert result["status"] == "ok"
    # Все 3 детектора должны быть в per_detector (или подмножество если split случайно отсёк)
    assert len(result["per_detector"]) >= 1
    for det, metrics in result["per_detector"].items():
        assert det in detectors
        assert "n_rows" in metrics
        assert "n_events" in metrics
        assert "per_row_brier" in metrics
        assert "per_event_brier" in metrics

    # train_detector_counts должен покрывать все 3 детектора (300 rows / 3 ≈ 100 каждый)
    assert len(result["train_detector_counts"]) == 3
    total_train = sum(result["train_detector_counts"].values())
    assert total_train == result["n_train_rows"]


def test_train_regime_counts_sums_to_n_train():
    """train_regime_counts.hf + .live = n_train_rows (caveat для repr-ности anchor)."""
    import numpy as np

    from app.jobs.ml_honest_baseline_fit import run_ml_honest_baseline_fit

    rng = np.random.default_rng(7)
    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    post_v2 = datetime(2026, 4, 29, tzinfo=UTC)
    rows = []
    for i in range(300):
        rows.append({
            "detector": "D1_fade_whale",
            "raw_p": float(rng.uniform(0.3, 0.7)),
            "expected_edge_pct": 10.0,
            "entry_price": 0.5,
            "horizon_sec": 1800,
            "generated_at": pre_v2 if i < 250 else post_v2,
            "outcome": "WIN" if rng.integers(0, 2) else "LOSS",
            "event_id": f"ev{i // 5}",
            "event_size": 5,
            "archetype": "single_market",
            "category": "Politics",
            "volume_24h": 10000.0,
        })

    async def fetch_mock(query, *args):
        if "ml_calibrators" in query:
            return []
        return rows

    with (
        patch("app.jobs.ml_honest_baseline_fit.fetch", new=AsyncMock(side_effect=fetch_mock)),
        patch("app.jobs.ml_honest_baseline_fit.execute", new=AsyncMock(return_value="INSERT 0 1")),
    ):
        result = asyncio.run(run_ml_honest_baseline_fit())

    counts = result["train_regime_counts"]
    assert counts["hf_pre_v2"] + counts["live_post_v2"] == result["n_train_rows"]


def test_persists_with_is_active_false():
    """is_active=false ВСЕГДА — honest baseline не активирует модель."""
    import numpy as np

    from app.jobs.ml_honest_baseline_fit import run_ml_honest_baseline_fit

    rng = np.random.default_rng(0)
    rows = [
        {
            "detector": "D1_fade_whale",
            "raw_p": float(rng.uniform(0.3, 0.7)),
            "expected_edge_pct": 10.0,
            "entry_price": 0.5,
            "horizon_sec": 1800,
            "generated_at": datetime(2026, 4, 1, tzinfo=UTC),
            "outcome": "WIN" if rng.integers(0, 2) else "LOSS",
            "event_id": f"ev{i // 5}",
            "event_size": 5,
            "archetype": "single_market",
            "category": "Politics",
            "volume_24h": 10000.0,
        }
        for i in range(150)
    ]

    captured_args: list = []

    async def fetch_mock(query, *args):
        if "ml_calibrators" in query:
            return []
        return rows

    async def execute_mock(query, *args):
        captured_args.append((query, args))
        return "INSERT 0 1"

    with (
        patch("app.jobs.ml_honest_baseline_fit.fetch", new=AsyncMock(side_effect=fetch_mock)),
        patch("app.jobs.ml_honest_baseline_fit.execute", new=AsyncMock(side_effect=execute_mock)),
    ):
        asyncio.run(run_ml_honest_baseline_fit())

    # Проверяем INSERT — последний параметр (is_active) НЕ должен быть в args (он hardcoded false в SQL)
    insert_calls = [c for c in captured_args if "INSERT INTO ml_meta_labeler" in c[0]]
    assert len(insert_calls) == 1
    sql = insert_calls[0][0]
    # Hard-coded `false` в VALUES, не из параметров
    assert "false" in sql.lower()
    assert "is_active" in sql


def test_payload_serializable_as_json():
    """Saved payload — валидный JSON (для ml_meta_labeler.model_payload jsonb)."""
    import numpy as np

    from app.jobs.ml_honest_baseline_fit import run_ml_honest_baseline_fit

    rng = np.random.default_rng(1)
    rows = [
        {
            "detector": "D1_fade_whale",
            "raw_p": float(rng.uniform(0.3, 0.7)),
            "expected_edge_pct": 10.0,
            "entry_price": 0.5,
            "horizon_sec": 1800,
            "generated_at": datetime(2026, 4, 1, tzinfo=UTC),
            "outcome": "WIN" if rng.integers(0, 2) else "LOSS",
            "event_id": f"ev{i // 5}",
            "event_size": 5,
            "archetype": "single_market",
            "category": "Politics",
            "volume_24h": 10000.0,
        }
        for i in range(150)
    ]

    captured: list = []

    async def fetch_mock(query, *args):
        if "ml_calibrators" in query:
            return []
        return rows

    async def execute_mock(query, *args):
        captured.append((query, args))
        return "INSERT 0 1"

    with (
        patch("app.jobs.ml_honest_baseline_fit.fetch", new=AsyncMock(side_effect=fetch_mock)),
        patch("app.jobs.ml_honest_baseline_fit.execute", new=AsyncMock(side_effect=execute_mock)),
    ):
        asyncio.run(run_ml_honest_baseline_fit())

    insert = [c for c in captured if "INSERT INTO ml_meta_labeler" in c[0]][0]
    args = insert[1]
    # args[1] = payload_json
    payload = args[1]
    parsed = json.loads(payload)
    assert "model_str" in parsed
    assert "feature_names" in parsed


@pytest.mark.parametrize("expected_key", [
    "status",
    "version",
    "n_train_rows",
    "n_holdout_rows",
    "n_events_holdout",
    "old_holdout_brier_per_row",
    "honest_holdout_per_event_brier",
    "hf_pre_v2",
    "live_post_v2",
    "per_detector",
    "train_detector_counts",
    "train_regime_counts",
    "config",
])
def test_summary_has_all_required_keys(expected_key):
    """Memory file template (из плана) ожидает эти keys в результате."""
    import numpy as np

    from app.jobs.ml_honest_baseline_fit import run_ml_honest_baseline_fit

    rng = np.random.default_rng(2)
    rows = [
        {
            "detector": "D1_fade_whale",
            "raw_p": float(rng.uniform(0.3, 0.7)),
            "expected_edge_pct": 10.0,
            "entry_price": 0.5,
            "horizon_sec": 1800,
            "generated_at": datetime(2026, 4, 1, tzinfo=UTC),
            "outcome": "WIN" if rng.integers(0, 2) else "LOSS",
            "event_id": f"ev{i // 5}",
            "event_size": 5,
            "archetype": "single_market",
            "category": "Politics",
            "volume_24h": 10000.0,
        }
        for i in range(150)
    ]

    async def fetch_mock(query, *args):
        if "ml_calibrators" in query:
            return []
        return rows

    with (
        patch("app.jobs.ml_honest_baseline_fit.fetch", new=AsyncMock(side_effect=fetch_mock)),
        patch("app.jobs.ml_honest_baseline_fit.execute", new=AsyncMock(return_value="INSERT 0 1")),
    ):
        result = asyncio.run(run_ml_honest_baseline_fit())

    assert expected_key in result
