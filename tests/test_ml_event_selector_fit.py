"""Tests для app/jobs/ml_event_selector_fit.py.

Stage 2 / Two-Stage ML Pipeline. Layer A fit cron tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch


def test_module_imports():
    from app.jobs.ml_event_selector_fit import run_ml_event_selector_fit

    assert asyncio.iscoroutinefunction(run_ml_event_selector_fit)


def test_v2_cutover_constant():
    from app.jobs.ml_event_selector_fit import V2_CUTOVER

    assert V2_CUTOVER == datetime(2026, 4, 28, tzinfo=UTC)


def test_constants():
    from app.jobs import ml_event_selector_fit as mod

    assert mod.ROLLING_DAYS == 365
    assert mod.MIN_TRAIN_EVENTS == 50
    assert mod.PROMOTION_BRIER_RATIO == 0.95


def test_too_few_events_returns_skipped():
    from app.jobs.ml_event_selector_fit import run_ml_event_selector_fit

    fake_rows = [{} for _ in range(10)]
    with patch("app.jobs.ml_event_selector_fit.fetch", new=AsyncMock(return_value=fake_rows)):
        result = asyncio.run(run_ml_event_selector_fit())
    assert result["status"] == "skipped"
    assert result["reason"] == "too_few_events"
    assert result["n"] == 10


def _mk_row(event_id: str, generated_at: datetime, event_roi: float, n_markets: int = 5) -> dict:
    return {
        "event_polymarket_id": event_id,
        "category": "Politics",
        "archetype": "single_market",
        "n_markets": n_markets,
        "volume_24h": 10000.0,
        "liquidity": 1000.0,
        "mean_market_price": 0.5,
        "market_price_dispersion": 0.05,
        "neg_risk": False,
        "neg_risk_other": False,
        "n_yes_winners": 1,
        "n_detector_signals": 2,
        "first_signal_at": generated_at,
        "days_to_resolution": 7.0,
        "event_roi": event_roi,
    }


def test_full_run_with_synthetic_data():
    """End-to-end на 100 events. Train + holdout, persist, promote."""
    import numpy as np

    from app.jobs.ml_event_selector_fit import run_ml_event_selector_fit

    rng = np.random.default_rng(42)
    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    post_v2 = datetime(2026, 4, 29, tzinfo=UTC)
    rows = []
    for i in range(120):
        rows.append(_mk_row(
            event_id=f"ev{i}",
            generated_at=pre_v2 if i < 100 else post_v2,
            event_roi=float(rng.uniform(-0.3, 0.3)),
        ))

    captured: list = []

    async def fetch_mock(query, *args):
        return rows

    async def execute_mock(query, *args):
        captured.append((query, args))
        return "INSERT 0 1"

    async def fetchrow_mock(query, *args):
        # No active model on first run
        return None

    with (
        patch("app.jobs.ml_event_selector_fit.fetch", new=AsyncMock(side_effect=fetch_mock)),
        patch("app.jobs.ml_event_selector_fit.execute", new=AsyncMock(side_effect=execute_mock)),
        patch("app.jobs.ml_event_selector_fit.fetchrow", new=AsyncMock(side_effect=fetchrow_mock)),
        # Validation gate отключён для synthetic data (random outcomes → fails BSS)
        patch("app.jobs.ml_event_selector_fit.VALIDATION_GATE_ENABLED", False),
    ):
        result = asyncio.run(run_ml_event_selector_fit())

    assert result["status"] == "ok"
    assert result["version"].startswith("event_selector_v")
    assert result["n_events_train"] + result["n_events_holdout"] == 120
    assert 0.0 <= result["holdout_per_event_brier"] <= 1.0
    # No active → promote (validation gate disabled)
    assert result["promoted"] is True
    assert result["promote_reason"] == "no_active_model"


def test_validation_gate_blocks_promote_on_random_data():
    """С включённым gate (default) random outcomes должны блокироваться.
    Synthetic data → BSS<0.05 → gate FAILS → promoted=False.
    """
    import numpy as np

    from app.jobs.ml_event_selector_fit import run_ml_event_selector_fit

    rng = np.random.default_rng(42)
    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    post_v2 = datetime(2026, 4, 29, tzinfo=UTC)
    rows = []
    for i in range(120):
        rows.append(_mk_row(
            event_id=f"ev{i}",
            generated_at=pre_v2 if i < 100 else post_v2,
            event_roi=float(rng.uniform(-0.3, 0.3)),
        ))

    async def fetch_mock(query, *args):
        return rows

    async def execute_mock(query, *args):
        return "INSERT 0 1"

    async def fetchrow_mock(query, *args):
        return None  # no active model

    with (
        patch("app.jobs.ml_event_selector_fit.fetch", new=AsyncMock(side_effect=fetch_mock)),
        patch("app.jobs.ml_event_selector_fit.execute", new=AsyncMock(side_effect=execute_mock)),
        patch("app.jobs.ml_event_selector_fit.fetchrow", new=AsyncMock(side_effect=fetchrow_mock)),
        # default VALIDATION_GATE_ENABLED=True
    ):
        result = asyncio.run(run_ml_event_selector_fit())

    assert result["status"] == "ok"
    # Gate должен заблокировать promote из-за low BSS / per-band AUC на random
    assert result["promoted"] is False
    assert "VALIDATION_GATE_FAILED" in result["promote_reason"]


def test_outcome_threshold_event_roi_positive():
    """outcome=1 если event_roi > 0, иначе 0."""
    from app.jobs.ml_event_selector_fit import _row_to_example

    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    pos = _row_to_example(_mk_row("ev_pos", pre_v2, event_roi=0.05))
    neg = _row_to_example(_mk_row("ev_neg", pre_v2, event_roi=-0.05))
    zero = _row_to_example(_mk_row("ev_zero", pre_v2, event_roi=0.0))
    assert pos[0].outcome == 1
    assert neg[0].outcome == 0
    assert zero[0].outcome == 0  # > 0 strict — zero is loss


def test_hf_live_split_in_summary():
    """summary должен содержать hf_holdout_brier + hf_n_events + live_holdout_brier + live_n_events."""
    import numpy as np

    from app.jobs.ml_event_selector_fit import run_ml_event_selector_fit

    rng = np.random.default_rng(7)
    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    post_v2 = datetime(2026, 4, 29, tzinfo=UTC)
    rows = []
    # 100 pre-V2 + 30 post-V2
    for i in range(100):
        rows.append(_mk_row(f"ev_hf_{i}", pre_v2, float(rng.uniform(-0.3, 0.3))))
    for i in range(30):
        rows.append(_mk_row(f"ev_live_{i}", post_v2, float(rng.uniform(-0.3, 0.3))))

    with (
        patch("app.jobs.ml_event_selector_fit.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.ml_event_selector_fit.execute", new=AsyncMock(return_value="INSERT 0 1")),
        patch("app.jobs.ml_event_selector_fit.fetchrow", new=AsyncMock(return_value=None)),
    ):
        result = asyncio.run(run_ml_event_selector_fit())

    assert "hf_holdout_brier" in result
    assert "hf_n_events" in result
    assert "live_holdout_brier" in result
    assert "live_n_events" in result
    # Total holdout = 26 (20% of 130). HF + Live = 26.
    assert result["hf_n_events"] + result["live_n_events"] == result["n_events_holdout"]
    # Both regimes должны быть представлены (potentially)
    assert result["hf_n_events"] >= 0
    assert result["live_n_events"] >= 0


def test_rescore_active_brier_uses_payload():
    """_rescore_active_brier десериализует active payload и считает Brier на данных examples."""
    import numpy as np

    from app.calibration.event_selector import EventSelectorConfig, train_event_selector
    from app.jobs.ml_event_selector_fit import _rescore_active_brier, _row_to_example

    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    rng = np.random.default_rng(1)
    pairs = [_row_to_example(_mk_row(f"ev{i}", pre_v2, float(rng.uniform(-0.3, 0.3)))) for i in range(80)]
    examples = [p[0] for p in pairs]
    timestamps = [p[1] for p in pairs]
    model = train_event_selector(examples, timestamps, config=EventSelectorConfig(min_train_events=50), holdout_fraction=0.2, seed=42)
    import json
    payload = json.dumps(model.serialize())

    brier = _rescore_active_brier(payload, examples)
    assert brier is not None
    assert 0.0 <= brier <= 1.0


def test_rescore_active_brier_none_on_bad_payload():
    """Битый/пустой payload → None (caller fallback)."""
    from app.jobs.ml_event_selector_fit import _rescore_active_brier, _row_to_example

    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    ex = [_row_to_example(_mk_row("ev0", pre_v2, 0.1))[0]]
    assert _rescore_active_brier("", ex) is None
    assert _rescore_active_brier("not-json", ex) is None


def test_rescore_active_brier_none_on_empty_examples():
    """Пустой набор examples → None."""
    from app.jobs.ml_event_selector_fit import _rescore_active_brier
    assert _rescore_active_brier('{"model_str": "x"}', []) is None


def test_promotion_blocked_when_active_better():
    """Если active модель имеет brier=0.10, и new brier=0.15 (worse), не promote."""
    import numpy as np

    from app.jobs.ml_event_selector_fit import run_ml_event_selector_fit

    rng = np.random.default_rng(0)
    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    rows = [
        _mk_row(f"ev{i}", pre_v2, float(rng.uniform(-0.3, 0.3)))
        for i in range(100)
    ]

    with (
        patch("app.jobs.ml_event_selector_fit.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.ml_event_selector_fit.execute", new=AsyncMock(return_value="INSERT 0 1")),
        patch(
            "app.jobs.ml_event_selector_fit.fetchrow",
            new=AsyncMock(return_value={"version": "old_v1", "holdout_per_event_brier": 0.10}),
        ),
    ):
        result = asyncio.run(run_ml_event_selector_fit())

    # Random outcomes → brier ~0.25 → не лучше чем active 0.10×0.95
    assert result["promoted"] is False


def test_gate_uses_rescored_active_on_live_holdout():
    """Gate A сравнивает live-Brier кандидата с live-Brier re-scored active (не frozen scalar)."""
    import json

    import numpy as np

    from app.calibration.event_selector import EventSelectorConfig, train_event_selector
    from app.jobs.ml_event_selector_fit import _row_to_example, run_ml_event_selector_fit

    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    post_v2 = datetime(2026, 4, 29, tzinfo=UTC)
    rng = np.random.default_rng(3)
    rows = [_mk_row(f"ev_hf{i}", pre_v2, float(rng.uniform(-0.3, 0.3))) for i in range(100)] + [_mk_row(f"ev_live{i}", post_v2, float(rng.uniform(-0.3, 0.3))) for i in range(40)]

    train_pairs = [_row_to_example(r) for r in rows]
    active_model = train_event_selector(
        [p[0] for p in train_pairs], [p[1] for p in train_pairs],
        config=EventSelectorConfig(min_train_events=50), holdout_fraction=0.2, seed=99,
    )
    active_payload = json.dumps(active_model.serialize())

    async def fetchrow_mock(query, *args):
        return {"version": "active_v1", "holdout_per_event_brier": 0.05, "model_payload": active_payload}

    with (
        patch("app.jobs.ml_event_selector_fit.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.ml_event_selector_fit.execute", new=AsyncMock(return_value="INSERT 0 1")),
        patch("app.jobs.ml_event_selector_fit.fetchrow", new=AsyncMock(side_effect=fetchrow_mock)),
        patch("app.jobs.ml_event_selector_fit.VALIDATION_GATE_ENABLED", False),
    ):
        result = asyncio.run(run_ml_event_selector_fit())

    assert result["status"] == "ok"
    assert "live" in result["promote_reason"].lower()


def test_gate_fallback_when_no_live_holdout():
    """live_n=0 (все события pre-V2) → fallback на frozen scalar, reason содержит 'fallback'."""
    import json

    import numpy as np

    from app.calibration.event_selector import EventSelectorConfig, train_event_selector
    from app.jobs.ml_event_selector_fit import _row_to_example, run_ml_event_selector_fit

    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    rng = np.random.default_rng(4)
    rows = [_mk_row(f"ev{i}", pre_v2, float(rng.uniform(-0.3, 0.3))) for i in range(100)]
    train_pairs = [_row_to_example(r) for r in rows]
    active_model = train_event_selector(
        [p[0] for p in train_pairs], [p[1] for p in train_pairs],
        config=EventSelectorConfig(min_train_events=50), holdout_fraction=0.2, seed=5,
    )
    active_payload = json.dumps(active_model.serialize())

    async def fetchrow_mock(query, *args):
        return {"version": "active_v1", "holdout_per_event_brier": 0.05, "model_payload": active_payload}

    with (
        patch("app.jobs.ml_event_selector_fit.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.ml_event_selector_fit.execute", new=AsyncMock(return_value="INSERT 0 1")),
        patch("app.jobs.ml_event_selector_fit.fetchrow", new=AsyncMock(side_effect=fetchrow_mock)),
        patch("app.jobs.ml_event_selector_fit.VALIDATION_GATE_ENABLED", False),
    ):
        result = asyncio.run(run_ml_event_selector_fit())

    assert result["status"] == "ok"
    assert "fallback" in result["promote_reason"].lower()
