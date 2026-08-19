"""Tests для app/jobs/ml_market_picker_fit.py.

Stage 3 / Two-Stage ML Pipeline. Layer B fit job tests.

Focus: 3-gate promotion logic (standard + HF×1.10 + unsafe categories).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch


def test_module_imports():
    from app.jobs.ml_market_picker_fit import run_ml_market_picker_fit

    assert asyncio.iscoroutinefunction(run_ml_market_picker_fit)


def test_constants():
    from app.jobs import ml_market_picker_fit as mod

    assert mod.LAYER_A_THRESHOLD == 0.50
    assert mod.HF_LIVE_RATIO_GATE == 1.10
    assert mod.NAIVE_COIN_BRIER == 0.25
    assert mod.UNSAFE_CATEGORIES == ("Tech & AI", "World & Economy")


def test_no_active_layer_a_returns_skipped():
    from app.jobs.ml_market_picker_fit import run_ml_market_picker_fit

    with patch("app.jobs.ml_market_picker_fit._load_active_layer_a", new=AsyncMock(return_value=None)):
        result = asyncio.run(run_ml_market_picker_fit())
    assert result["status"] == "skipped"
    assert result["reason"] == "no_active_layer_a"


def test_3gate_no_active_promotes():
    """No active model → promote regardless."""
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, reasons = _evaluate_3gate_promotion(
        new_brier=0.20,
        active_brier=None,
        hf_brier=None,
        hf_n=0,
        live_brier=None,
        live_n=0,
        per_category={},
    )
    assert promote is True
    assert reasons == []


def test_3gate_standard_block():
    """active_brier × 0.95 < new_brier → blocked."""
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, reasons = _evaluate_3gate_promotion(
        new_brier=0.20,
        active_brier=0.20,  # 0.20 × 0.95 = 0.19; new 0.20 not less → block
        hf_brier=None,
        hf_n=0,
        live_brier=None,
        live_n=0,
        per_category={},
    )
    assert promote is False
    assert any("standard" in r for r in reasons)


def test_3gate_standard_pass_when_better():
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, reasons = _evaluate_3gate_promotion(
        new_brier=0.18,  # < 0.20 × 0.95 = 0.19 → pass
        active_brier=0.20,
        hf_brier=None,
        hf_n=0,
        live_brier=None,
        live_n=0,
        per_category={},
    )
    assert promote is True
    assert reasons == []


def test_3gate_hf_overfit_blocks():
    """live_brier > hf_brier × 1.10 (with sufficient n) → blocked."""
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, reasons = _evaluate_3gate_promotion(
        new_brier=0.18,  # standard pass
        active_brier=0.20,
        hf_brier=0.15,
        hf_n=50,
        live_brier=0.18,  # 0.15 × 1.10 = 0.165; live 0.18 > 0.165 → block
        live_n=50,
        per_category={},
    )
    assert promote is False
    assert any("hf_overfit" in r for r in reasons)


def test_3gate_hf_overfit_skipped_when_low_n():
    """live_n < 30 → HF gate skipped (no statistical power)."""
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, reasons = _evaluate_3gate_promotion(
        new_brier=0.18,
        active_brier=0.20,
        hf_brier=0.15,
        hf_n=50,
        live_brier=0.50,  # was bad but не считается
        live_n=10,  # < 30
        per_category={},
    )
    assert promote is True
    assert not any("hf_overfit" in r for r in reasons)


def test_3gate_unsafe_category_blocks():
    """Tech & AI brier > 0.25 with n_events ≥ 10 → blocked."""
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, reasons = _evaluate_3gate_promotion(
        new_brier=0.18,
        active_brier=0.20,
        hf_brier=None,
        hf_n=0,
        live_brier=None,
        live_n=0,
        per_category={
            "Tech & AI": {"brier": 0.30, "n_events": 15, "n_rows": 50, "win_rate": 0.5},
        },
    )
    assert promote is False
    assert any("unsafe_category[Tech & AI]" in r for r in reasons)


def test_3gate_unsafe_category_exempt_low_n():
    """Tech & AI с brier > 0.25 НО n_events < 10 → exempt (не блокирует)."""
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, _ = _evaluate_3gate_promotion(
        new_brier=0.18,
        active_brier=0.20,
        hf_brier=None,
        hf_n=0,
        live_brier=None,
        live_n=0,
        per_category={
            "Tech & AI": {"brier": 0.30, "n_events": 5, "n_rows": 5, "win_rate": 0.5},
        },
    )
    assert promote is True


def test_3gate_safe_category_with_high_brier_passes():
    """Категория не в UNSAFE_CATEGORIES — high brier не блокирует (Sports etc allowed)."""
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, _ = _evaluate_3gate_promotion(
        new_brier=0.18,
        active_brier=0.20,
        hf_brier=None,
        hf_n=0,
        live_brier=None,
        live_n=0,
        per_category={
            "Sports": {"brier": 0.40, "n_events": 100, "n_rows": 500, "win_rate": 0.5},
        },
    )
    assert promote is True


def test_3gate_uses_live_rescore_when_available():
    """Gate A: при наличии live re-score сравнивает live-vs-live, reason содержит 'live'."""
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, reasons = _evaluate_3gate_promotion(
        new_brier=0.30,
        active_brier=0.05,
        hf_brier=None, hf_n=0,
        live_brier=0.14, live_n=50,
        per_category={},
        active_live_brier=0.20,
        candidate_live_brier=0.14,
    )
    assert promote is True, f"должен promote по live-сравнению, reasons={reasons}"


def test_3gate_live_rescore_blocks_when_candidate_worse():
    """Gate A live: кандидат хуже re-scored active → block, reason содержит 'live'."""
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, reasons = _evaluate_3gate_promotion(
        new_brier=0.10,
        active_brier=0.50,
        hf_brier=None, hf_n=0,
        live_brier=0.25, live_n=50,
        per_category={},
        active_live_brier=0.20,
        candidate_live_brier=0.25,
    )
    assert promote is False
    assert any("live" in r for r in reasons), f"reason должен упоминать live: {reasons}"


def test_3gate_fallback_to_scalar_when_no_live_rescore():
    """Без live re-score (None) → fallback на frozen scalar на общем holdout."""
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    promote, reasons = _evaluate_3gate_promotion(
        new_brier=0.30,
        active_brier=0.05,
        hf_brier=None, hf_n=0,
        live_brier=None, live_n=0,
        per_category={},
        active_live_brier=None,
        candidate_live_brier=None,
    )
    assert promote is False
    assert any("fallback" in r for r in reasons), f"reason должен упоминать fallback: {reasons}"


def test_too_few_rows_returns_skipped():
    """Если rows < MIN_TRAIN_ROWS → skipped."""
    from app.jobs.ml_market_picker_fit import run_ml_market_picker_fit

    fake_layer_a = AsyncMock()
    with (
        patch("app.jobs.ml_market_picker_fit._load_active_layer_a", new=AsyncMock(return_value=fake_layer_a)),
        patch("app.jobs.ml_market_picker_fit.fetch", new=AsyncMock(return_value=[])),
    ):
        result = asyncio.run(run_ml_market_picker_fit())
    assert result["status"] == "skipped"
    assert result["reason"] == "too_few_rows"


def test_full_run_with_synthetic_data():
    """End-to-end: Layer A predicts, filter, train Layer B, promote."""
    import numpy as np

    from app.jobs.ml_market_picker_fit import run_ml_market_picker_fit

    rng = np.random.default_rng(42)
    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    rows = []
    # 60 events × 5 markets = 300 rows
    for i in range(300):
        rows.append({
            "signal_id": f"s{i}",
            "detector": "D1_fade_whale",
            "market_id": f"m{i}",
            "event_polymarket_id": f"ev{i // 5}",
            "side": "NO",
            "raw_p": float(rng.uniform(0.3, 0.7)),
            "expected_edge_pct": 10.0,
            "entry_price": 0.5,
            "horizon_sec": 1800,
            "generated_at": pre_v2,
            "outcome": "WIN" if rng.integers(0, 2) else "LOSS",
            "event_size": 5,
            "archetype": "single_market",
            "event_category": "Politics",
            "event_volume_24h": 50000.0,
            "event_liquidity": 5000.0,
            "mean_market_price": 0.5,
            "market_price_dispersion": 0.05,
            "neg_risk": False,
            "neg_risk_other": False,
            "n_yes_winners": 1,
            "n_markets": 5,
            "n_detector_signals": 5,
            "first_signal_at": pre_v2,
            "days_to_resolution": 7.0,
            "market_category": "Politics",
            "market_volume_24h": 10000.0,
        })

    # Mock Layer A — все p_event=0.7 (выше threshold)
    layer_a_mock = AsyncMock()
    layer_a_mock.predict = lambda examples: np.full(len(examples), 0.7)

    async def fetch_mock(query, *args):
        if "ml_calibrators" in query:
            return []
        return rows

    captured_inserts: list = []

    async def execute_mock(query, *args):
        captured_inserts.append((query, args))
        return "INSERT 0 1"

    with (
        patch("app.jobs.ml_market_picker_fit._load_active_layer_a", new=AsyncMock(return_value=layer_a_mock)),
        patch("app.jobs.ml_market_picker_fit.fetch", new=AsyncMock(side_effect=fetch_mock)),
        patch("app.jobs.ml_market_picker_fit.execute", new=AsyncMock(side_effect=execute_mock)),
        patch("app.jobs.ml_market_picker_fit.fetchrow", new=AsyncMock(return_value=None)),
        # Validation gate отключён для synthetic data (random outcomes → fails BSS/cal slope)
        patch("app.jobs.ml_market_picker_fit.VALIDATION_GATE_ENABLED", False),
    ):
        result = asyncio.run(run_ml_market_picker_fit())

    assert result["status"] == "ok"
    assert result["version"].startswith("market_picker_v")
    assert result["n_train_rows"] + result["n_holdout_rows"] == 300
    assert result["promoted"] is True  # No active + gate disabled → promote
    assert "per_category_metrics" in result
    assert "Politics" in result["per_category_metrics"]


def test_layer_a_filter_drops_low_p_event():
    """Когда Layer A возвращает p_event<threshold для всех — universe пуст."""
    import numpy as np

    from app.jobs.ml_market_picker_fit import run_ml_market_picker_fit

    rng = np.random.default_rng(0)
    pre_v2 = datetime(2026, 4, 1, tzinfo=UTC)
    rows = [
        {
            "signal_id": f"s{i}",
            "detector": "D1_fade_whale",
            "market_id": f"m{i}",
            "event_polymarket_id": f"ev{i // 5}",
            "side": "NO",
            "raw_p": 0.5,
            "expected_edge_pct": 10.0,
            "entry_price": 0.5,
            "horizon_sec": 1800,
            "generated_at": pre_v2,
            "outcome": "WIN" if rng.integers(0, 2) else "LOSS",
            "event_size": 5,
            "archetype": "single_market",
            "event_category": "Politics",
            "event_volume_24h": 50000.0,
            "event_liquidity": 5000.0,
            "mean_market_price": 0.5,
            "market_price_dispersion": 0.05,
            "neg_risk": False,
            "neg_risk_other": False,
            "n_yes_winners": 1,
            "n_markets": 5,
            "n_detector_signals": 5,
            "first_signal_at": pre_v2,
            "days_to_resolution": 7.0,
            "market_category": "Politics",
            "market_volume_24h": 10000.0,
        }
        for i in range(300)
    ]

    # Layer A returns p_event=0.1 (below 0.5 threshold) — все отфильтрованы
    layer_a_mock = AsyncMock()
    layer_a_mock.predict = lambda examples: np.full(len(examples), 0.1)

    async def fetch_mock(query, *args):
        if "ml_calibrators" in query:
            return []
        return rows

    with (
        patch("app.jobs.ml_market_picker_fit._load_active_layer_a", new=AsyncMock(return_value=layer_a_mock)),
        patch("app.jobs.ml_market_picker_fit.fetch", new=AsyncMock(side_effect=fetch_mock)),
    ):
        result = asyncio.run(run_ml_market_picker_fit())

    assert result["status"] == "skipped"
    assert result["reason"] == "too_few_after_layer_a_filter"
    assert result["threshold"] == 0.50


def test_rescore_active_brier_picker_uses_payload():
    """_rescore_active_brier_picker десериализует Layer B payload, считает per-event Brier через per_event_brier."""
    import json

    import numpy as np

    from app.calibration.market_picker import MarketPickerExample, train_market_picker
    from app.jobs.ml_market_picker_fit import _rescore_active_brier_picker

    rng = np.random.default_rng(11)
    examples = []
    event_ids = []
    for ev in range(40):
        for _mk in range(3):
            eid = f"ev{ev}"
            examples.append(
                MarketPickerExample(
                    detector_id="D3",
                    calibrated_p=float(rng.uniform(0.3, 0.7)),
                    edge_pct=float(rng.uniform(1.0, 20.0)),
                    entry_price=float(rng.uniform(0.3, 0.7)),
                    horizon_sec=14400,
                    category="Politics",
                    volume_24h=10000.0,
                    is_v2_post=True,
                    p_event=0.6,
                    archetype="single_market",
                    n_markets_in_event=3,
                    outcome=int(rng.integers(0, 2)),
                )
            )
            event_ids.append(eid)
    # train_market_picker requires event_ids as a positional arg (REQUIRED для GroupKFold)
    model = train_market_picker(examples, event_ids, holdout_fraction=0.2, seed=42)
    payload = json.dumps(model.serialize())

    brier = _rescore_active_brier_picker(payload, examples, event_ids)
    assert brier is not None
    assert 0.0 <= brier <= 1.0


def test_rescore_active_brier_picker_aggregates_per_event_not_per_row():
    """Helper должен агрегировать per-event (через per_event_brier), НЕ per-row.

    Ловит регрессию к naive per-row Brier: при multi-market events с mixed outcomes
    per-event и per-row дают РАЗНЫЕ значения. Helper обязан совпадать с per-event.
    """
    import json

    import numpy as np

    from app.calibration.market_picker import MarketPickerExample, train_market_picker
    from app.calibration.per_event_metrics import per_event_brier
    from app.jobs.ml_market_picker_fit import _rescore_active_brier_picker

    rng = np.random.default_rng(11)
    examples = []
    event_ids = []
    # 40 events × 3 markets, mixed outcomes within event → per-event != per-row
    for ev in range(40):
        for mk in range(3):
            eid = f"ev{ev}"
            examples.append(
                MarketPickerExample(
                    detector_id="D3",
                    calibrated_p=float(rng.uniform(0.3, 0.7)),
                    edge_pct=float(rng.uniform(1.0, 20.0)),
                    entry_price=float(rng.uniform(0.3, 0.7)),
                    horizon_sec=14400,
                    category="Politics",
                    volume_24h=10000.0,
                    is_v2_post=True,
                    p_event=0.6,
                    archetype="single_market",
                    n_markets_in_event=3,
                    outcome=mk % 2,  # mixed within event: [0,1,0] → mean_y per event != 0/1
                )
            )
            event_ids.append(eid)
    model = train_market_picker(examples, event_ids, holdout_fraction=0.2, seed=42)
    payload = json.dumps(model.serialize())

    helper_brier = _rescore_active_brier_picker(payload, examples, event_ids)

    # Independent per-event computation on the SAME predictions
    preds = [float(p) for p in model.predict(examples)]
    outcomes = [int(ex.outcome) for ex in examples]
    expected_per_event = per_event_brier(event_ids, preds, outcomes)
    naive_per_row = float(np.mean([(p - y) ** 2 for p, y in zip(preds, outcomes)]))

    # Helper must match per-event aggregation exactly
    assert helper_brier == expected_per_event
    # And per-event must DIFFER from naive per-row (proves aggregation matters here)
    assert abs(expected_per_event - naive_per_row) > 1e-6, (
        f"test setup invalid: per-event {expected_per_event} == per-row {naive_per_row}, "
        "events don't have mixed-outcome multi-market structure"
    )


def test_rescore_active_brier_picker_none_on_bad_payload():
    """Битый/пустой payload или пустые examples → None."""
    from app.jobs.ml_market_picker_fit import _rescore_active_brier_picker

    assert _rescore_active_brier_picker("", [], []) is None
    assert _rescore_active_brier_picker("not-json", [], []) is None
    assert _rescore_active_brier_picker('{"model_str": "x"}', [], []) is None
