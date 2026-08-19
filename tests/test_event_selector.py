"""Tests для app/calibration/event_selector.py.

Stage 2 / Two-Stage ML Pipeline. Layer A = Event Selector.

Per-event row (НЕ per-market). 1 row = 1 event. Time-based split.
"""

from __future__ import annotations

import numpy as np
import pytest


def _mk_examples(n: int = 100, seed: int = 42):
    """Синтетические EventSelectorExample для unit тестов."""
    from app.calibration.event_selector import EventSelectorExample

    rng = np.random.default_rng(seed)
    examples = []
    timestamps = []
    for i in range(n):
        examples.append(
            EventSelectorExample(
                event_id=f"ev{i}",
                category="Politics",
                archetype="single_market",
                n_markets=int(rng.integers(1, 10)),
                volume_24h=float(rng.uniform(1000, 50000)),
                liquidity=float(rng.uniform(100, 5000)),
                days_to_resolution=float(rng.uniform(1, 30)),
                mean_market_price=float(rng.uniform(0.1, 0.9)),
                market_price_dispersion=float(rng.uniform(0, 0.3)),
                neg_risk=False,
                neg_risk_other=False,
                n_yes_winners=1,
                n_detector_signals=int(rng.integers(1, 5)),
                outcome=int(rng.integers(0, 2)),
            )
        )
        timestamps.append(float(i))
    return examples, timestamps


def test_known_archetypes_match_db_check_constraint():
    """Whitelist archetype values из event_selector должен совпадать с DB CHECK."""
    from app.calibration.event_selector import KNOWN_ARCHETYPES

    expected = (
        "mutually_exclusive_ladder",
        "one_of_n_choice",
        "independent_multi",
        "hierarchical_time",
        "single_market",
        "unclassified",
    )
    assert KNOWN_ARCHETYPES == expected


def test_known_categories_match_meta_labeler():
    """Категории должны быть консистентны с meta_labeler чтобы фичи переносились."""
    from app.calibration.event_selector import KNOWN_CATEGORIES_A
    from app.calibration.meta_labeler import KNOWN_CATEGORIES

    assert set(KNOWN_CATEGORIES_A) == set(KNOWN_CATEGORIES)


def test_example_dataclass_required_fields():
    from app.calibration.event_selector import EventSelectorExample

    ex = EventSelectorExample(
        event_id="ev1",
        category="Politics",
        archetype="single_market",
        n_markets=1,
        volume_24h=10000.0,
        liquidity=5000.0,
        days_to_resolution=7.0,
        mean_market_price=0.5,
        market_price_dispersion=0.0,
        neg_risk=False,
        neg_risk_other=False,
        n_yes_winners=1,
        n_detector_signals=2,
        outcome=1,
    )
    assert ex.event_id == "ev1"
    assert ex.outcome == 1


def test_train_returns_model_with_metrics():
    from app.calibration.event_selector import EventSelectorConfig, train_event_selector

    examples, ts = _mk_examples(100)
    cfg = EventSelectorConfig(min_train_events=20)
    model = train_event_selector(examples, ts, config=cfg, holdout_fraction=0.2)
    assert model.n_events_train + model.n_events_holdout == 100
    assert 0.0 <= model.holdout_per_event_brier <= 1.0
    assert 0.0 <= model.train_per_event_brier <= 1.0


def test_too_few_events_raises():
    from app.calibration.event_selector import EventSelectorConfig, train_event_selector

    examples, ts = _mk_examples(10)
    cfg = EventSelectorConfig(min_train_events=20)
    with pytest.raises(ValueError, match="min_train_events"):
        train_event_selector(examples, ts, config=cfg)


def test_time_based_split_holdout_is_late():
    """Holdout содержит events с большими timestamps (newer events)."""
    from app.calibration.event_selector import EventSelectorConfig, train_event_selector

    examples, ts = _mk_examples(100)
    # ts[i] = float(i) — strictly increasing
    cfg = EventSelectorConfig(min_train_events=20)
    model = train_event_selector(examples, ts, config=cfg, holdout_fraction=0.2)
    # 100 events, 20% = 20 last → events 80..99 в holdout
    assert model.n_events_holdout == 20
    assert model.n_events_train == 80


def test_serialize_roundtrip():
    from app.calibration.event_selector import (
        EventSelectorConfig,
        deserialize_from_json,
        serialize_to_json,
        train_event_selector,
    )

    examples, ts = _mk_examples(100)
    cfg = EventSelectorConfig(min_train_events=20)
    model = train_event_selector(examples, ts, config=cfg)
    s = serialize_to_json(model)
    restored = deserialize_from_json(s)
    p1 = model.predict(examples[:5])
    p2 = restored.predict(examples[:5])
    np.testing.assert_array_almost_equal(p1, p2, decimal=6)


def test_predict_returns_probabilities_in_unit_interval():
    from app.calibration.event_selector import EventSelectorConfig, train_event_selector

    examples, ts = _mk_examples(100)
    cfg = EventSelectorConfig(min_train_events=20)
    model = train_event_selector(examples, ts, config=cfg)
    preds = model.predict(examples[:20])
    assert preds.shape == (20,)
    assert (preds >= 0.0).all()
    assert (preds <= 1.0).all()


def test_predict_one_returns_scalar():
    from app.calibration.event_selector import EventSelectorConfig, train_event_selector

    examples, ts = _mk_examples(100)
    cfg = EventSelectorConfig(min_train_events=20)
    model = train_event_selector(examples, ts, config=cfg)
    p = model.predict_one(examples[0])
    assert isinstance(p, float)
    assert 0.0 <= p <= 1.0


def test_unknown_archetype_zero_one_hot():
    """Unknown archetype value (например new value not in KNOWN_*) → all-zero one-hot."""
    from app.calibration.event_selector import (
        EventSelectorConfig,
        EventSelectorExample,
        train_event_selector,
    )

    examples, ts = _mk_examples(100)
    cfg = EventSelectorConfig(min_train_events=20)
    model = train_event_selector(examples, ts, config=cfg)

    # Create example с unknown archetype/category
    weird = EventSelectorExample(
        event_id="weird",
        category="UnknownCategory",
        archetype="future_archetype_v2",
        n_markets=5,
        volume_24h=10000.0,
        liquidity=1000.0,
        days_to_resolution=7.0,
        mean_market_price=0.5,
        market_price_dispersion=0.05,
        neg_risk=False,
        neg_risk_other=False,
        n_yes_winners=1,
        n_detector_signals=2,
        outcome=0,
    )
    # Не должно падать — просто all-zero one-hot для unknown
    p = model.predict_one(weird)
    assert 0.0 <= p <= 1.0


def test_timestamp_length_mismatch_raises():
    from app.calibration.event_selector import EventSelectorConfig, train_event_selector

    examples, _ = _mk_examples(100)
    cfg = EventSelectorConfig(min_train_events=20)
    with pytest.raises(ValueError, match="len mismatch"):
        train_event_selector(examples, [1.0, 2.0], config=cfg)
