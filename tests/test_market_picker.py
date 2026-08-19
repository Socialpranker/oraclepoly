"""Tests для app/calibration/market_picker.py.

Stage 3 / Two-Stage ML Pipeline. Layer B = Market+Side Picker.

Per-row model (1 row = 1 detector signal), но обучение event-aware:
- GroupKFold по event_id (markets одного event'а в одной части)
- sample_weight = 1/event_size

Critical property: top1_per_event_accuracy — модель выбирает winner внутри event'а.
"""

from __future__ import annotations

import numpy as np


def _mk(n: int = 200, seed: int = 42, event_size: int = 5):
    """Synthetic MarketPickerExamples + event_ids + 1/event_size weights."""
    from app.calibration.market_picker import MarketPickerExample

    rng = np.random.default_rng(seed)
    examples = []
    event_ids = []
    weights = []
    for i in range(n):
        ev = f"ev{i // event_size}"
        examples.append(
            MarketPickerExample(
                detector_id="D1_fade_whale",
                calibrated_p=float(rng.uniform(0.3, 0.7)),
                edge_pct=10.0,
                entry_price=0.5,
                horizon_sec=1800,
                category="Politics",
                volume_24h=10000.0,
                is_v2_post=False,
                p_event=float(rng.uniform(0.4, 0.8)),
                archetype="single_market",
                n_markets_in_event=event_size,
                outcome=int(rng.integers(0, 2)),
            )
        )
        event_ids.append(ev)
        weights.append(1.0 / event_size)
    return examples, event_ids, weights


def test_known_detectors_match_meta_labeler():
    from app.calibration.market_picker import KNOWN_DETECTORS_B
    from app.calibration.meta_labeler import KNOWN_DETECTORS

    assert set(KNOWN_DETECTORS_B) == set(KNOWN_DETECTORS)


def test_known_archetypes_match_event_selector():
    from app.calibration.event_selector import KNOWN_ARCHETYPES
    from app.calibration.market_picker import KNOWN_ARCHETYPES_B

    assert set(KNOWN_ARCHETYPES_B) == set(KNOWN_ARCHETYPES)


def test_train_with_groupkfold_and_weights_no_leakage():
    """train_market_picker — markets одного event'а в одной части."""
    from app.calibration.market_picker import MarketPickerConfig, train_market_picker

    examples, event_ids, weights = _mk(200, event_size=5)
    cfg = MarketPickerConfig(min_train_rows=50)
    model = train_market_picker(
        examples,
        event_ids=event_ids,
        sample_weight=weights,
        config=cfg,
        holdout_fraction=0.20,
        seed=42,
    )
    assert model.n_train_rows + model.n_holdout_rows == 200

    # Никакой event не в обоих частях
    train_evs = {event_ids[i] for i in model.train_indices}
    holdout_evs = {event_ids[i] for i in model.holdout_indices}
    assert train_evs.isdisjoint(holdout_evs), "GroupKFold leakage detected"


def test_top1_per_event_accuracy_perfect():
    """Когда model выбрала именно WIN row в каждом event'е → accuracy=1.0."""
    from app.calibration.market_picker import top1_per_event_accuracy

    event_ids = ["ev1", "ev1", "ev1", "ev2", "ev2"]
    preds = [0.6, 0.7, 0.5, 0.4, 0.8]
    outcomes = [0, 1, 0, 0, 1]
    # ev1: max p at idx=1, outcome=1 → hit
    # ev2: max p at idx=4, outcome=1 → hit
    assert top1_per_event_accuracy(event_ids, preds, outcomes) == 1.0


def test_top1_per_event_accuracy_miss():
    from app.calibration.market_picker import top1_per_event_accuracy

    event_ids = ["ev1", "ev1"]
    preds = [0.7, 0.3]
    outcomes = [0, 1]  # win at idx=1, model выбрала idx=0 → miss
    assert top1_per_event_accuracy(event_ids, preds, outcomes) == 0.0


def test_top1_skip_events_without_winner():
    """Events без выигрышей пропускаются — нечего угадывать."""
    from app.calibration.market_picker import top1_per_event_accuracy

    event_ids = ["ev1", "ev1", "ev2", "ev2"]
    preds = [0.8, 0.7, 0.6, 0.5]
    outcomes = [0, 0, 1, 0]  # ev1: no winner; ev2: winner at idx=2 (max pred = idx=2)
    assert top1_per_event_accuracy(event_ids, preds, outcomes) == 1.0


def test_top1_returns_zero_when_no_eligible_events():
    from app.calibration.market_picker import top1_per_event_accuracy

    event_ids = ["ev1", "ev1"]
    preds = [0.5, 0.4]
    outcomes = [0, 0]  # no winners
    assert top1_per_event_accuracy(event_ids, preds, outcomes) == 0.0


def test_serialize_roundtrip():
    from app.calibration.market_picker import (
        MarketPickerConfig,
        deserialize_from_json,
        serialize_to_json,
        train_market_picker,
    )

    examples, event_ids, weights = _mk(200, event_size=5)
    cfg = MarketPickerConfig(min_train_rows=50)
    model = train_market_picker(examples, event_ids=event_ids, sample_weight=weights, config=cfg)
    s = serialize_to_json(model)
    restored = deserialize_from_json(s)
    p1 = model.predict(examples[:5])
    p2 = restored.predict(examples[:5])
    np.testing.assert_array_almost_equal(p1, p2, decimal=6)


def test_predict_returns_probabilities_in_unit_interval():
    from app.calibration.market_picker import MarketPickerConfig, train_market_picker

    examples, event_ids, weights = _mk(200, event_size=5)
    cfg = MarketPickerConfig(min_train_rows=50)
    model = train_market_picker(examples, event_ids=event_ids, sample_weight=weights, config=cfg)
    preds = model.predict(examples[:20])
    assert preds.shape == (20,)
    assert (preds >= 0.0).all()
    assert (preds <= 1.0).all()


def test_too_few_rows_raises():
    import pytest

    from app.calibration.market_picker import MarketPickerConfig, train_market_picker

    examples, event_ids, weights = _mk(50, event_size=5)
    cfg = MarketPickerConfig(min_train_rows=100)
    with pytest.raises(ValueError, match="min_train_rows"):
        train_market_picker(examples, event_ids=event_ids, sample_weight=weights, config=cfg)


def test_event_ids_length_mismatch_raises():
    import pytest

    from app.calibration.market_picker import MarketPickerConfig, train_market_picker

    examples, _, weights = _mk(200, event_size=5)
    cfg = MarketPickerConfig(min_train_rows=50)
    with pytest.raises(ValueError, match="len mismatch"):
        train_market_picker(examples, event_ids=["ev1"], sample_weight=weights, config=cfg)


def test_unknown_detector_handled():
    """Inference на example с unknown detector_id → all-zero one-hot."""
    from app.calibration.market_picker import MarketPickerConfig, MarketPickerExample, train_market_picker

    examples, event_ids, weights = _mk(200, event_size=5)
    cfg = MarketPickerConfig(min_train_rows=50)
    model = train_market_picker(examples, event_ids=event_ids, sample_weight=weights, config=cfg)

    weird = MarketPickerExample(
        detector_id="D99_future_detector",
        calibrated_p=0.6,
        edge_pct=10.0,
        entry_price=0.5,
        horizon_sec=1800,
        category="UnknownCategory",
        volume_24h=10000.0,
        is_v2_post=True,
        p_event=0.7,
        archetype="future_archetype",
        n_markets_in_event=5,
        outcome=0,
    )
    p = model.predict_one(weird)
    assert 0.0 <= p <= 1.0


# ─── EV-regression mode (Shadow A/B) ─────────────────────────────────────────


def _mk_ev(n: int = 200, seed: int = 42, event_size: int = 5):
    """Synthetic examples с net_roi_target для EV-regression mode."""
    from app.calibration.market_picker import MarketPickerExample

    rng = np.random.default_rng(seed)
    examples = []
    event_ids = []
    weights = []
    for i in range(n):
        ev = f"ev{i // event_size}"
        # Симулируем: low entry → high realized roi; high entry → low/negative
        entry = float(rng.uniform(0.2, 0.8))
        # WIN ROI: 1/entry - 1; LOSS ROI: -1; binary outcome
        is_win = rng.random() < (1.0 - entry)  # lower entry → more wins
        net_roi = (1.0 / max(entry, 0.05) - 1.0) if is_win else -1.0
        # Add noise
        net_roi += float(rng.normal(0, 0.1))
        examples.append(
            MarketPickerExample(
                detector_id="D1_fade_whale",
                calibrated_p=float(rng.uniform(0.3, 0.7)),
                edge_pct=10.0,
                entry_price=entry,
                horizon_sec=1800,
                category="Politics",
                volume_24h=10000.0,
                is_v2_post=False,
                p_event=float(rng.uniform(0.4, 0.8)),
                archetype="single_market",
                n_markets_in_event=event_size,
                outcome=1 if is_win else 0,
                net_roi_target=net_roi,
            )
        )
        event_ids.append(ev)
        weights.append(1.0 / event_size)
    return examples, event_ids, weights


def test_ev_regression_config_factory():
    from app.calibration.market_picker import MarketPickerConfig

    cfg = MarketPickerConfig.for_ev_regression()
    assert cfg.mode == "ev_regression"
    cfg_default = MarketPickerConfig()
    assert cfg_default.mode == "binary"


def test_ev_regression_train_succeeds():
    from app.calibration.market_picker import MarketPickerConfig, train_market_picker

    examples, event_ids, weights = _mk_ev(200, event_size=5)
    cfg = MarketPickerConfig.for_ev_regression()
    cfg = MarketPickerConfig(min_train_rows=50, mode="ev_regression")
    model = train_market_picker(examples, event_ids=event_ids, sample_weight=weights, config=cfg)
    assert model.mode == "ev_regression"
    assert model.n_train_rows + model.n_holdout_rows == 200


def test_ev_regression_predict_returns_continuous():
    """Regression mode → выход не [0,1], а [EV_TARGET_CLIP_LO, EV_TARGET_CLIP_HI]."""
    from app.calibration.market_picker import (
        EV_TARGET_CLIP_HI,
        EV_TARGET_CLIP_LO,
        MarketPickerConfig,
        train_market_picker,
    )

    examples, event_ids, weights = _mk_ev(300, event_size=5)
    cfg = MarketPickerConfig(min_train_rows=50, mode="ev_regression")
    model = train_market_picker(examples, event_ids=event_ids, sample_weight=weights, config=cfg)
    preds = model.predict(examples[:30])
    assert preds.shape == (30,)
    # Не должны быть прижаты к [0, 1] — это regression
    assert preds.min() >= EV_TARGET_CLIP_LO * 1.5  # запас на noise
    assert preds.max() <= EV_TARGET_CLIP_HI * 1.5


def test_ev_regression_requires_net_roi_target():
    import pytest

    from app.calibration.market_picker import MarketPickerConfig, MarketPickerExample, train_market_picker

    # Examples без net_roi_target
    examples = [
        MarketPickerExample(
            detector_id="D1_fade_whale",
            calibrated_p=0.5,
            edge_pct=10.0,
            entry_price=0.5,
            horizon_sec=1800,
            category="Politics",
            volume_24h=10000.0,
            is_v2_post=False,
            p_event=0.5,
            archetype="single_market",
            n_markets_in_event=1,
            outcome=1,
            net_roi_target=None,  # ← missing
        )
        for _ in range(60)
    ]
    cfg = MarketPickerConfig(min_train_rows=50, mode="ev_regression")
    with pytest.raises(ValueError, match="ev_regression requires net_roi_target"):
        train_market_picker(examples, event_ids=[f"ev{i}" for i in range(60)], sample_weight=None, config=cfg)


def test_ev_regression_serialize_preserves_mode():
    from app.calibration.market_picker import (
        MarketPickerConfig,
        deserialize_from_json,
        serialize_to_json,
        train_market_picker,
    )

    examples, event_ids, weights = _mk_ev(200, event_size=5)
    cfg = MarketPickerConfig(min_train_rows=50, mode="ev_regression")
    model = train_market_picker(examples, event_ids=event_ids, sample_weight=weights, config=cfg)
    s = serialize_to_json(model)
    restored = deserialize_from_json(s)
    assert restored.mode == "ev_regression"
    p1 = model.predict(examples[:5])
    p2 = restored.predict(examples[:5])
    np.testing.assert_array_almost_equal(p1, p2, decimal=6)
