"""Тесты L2 LightGBM meta-labeler."""

from __future__ import annotations

import random

import numpy as np
import pytest

from app.calibration.meta_labeler import (
    KNOWN_CATEGORIES,
    KNOWN_DETECTORS,
    MetaLabelerConfig,
    MetaLabelerExample,
    _vectorize,
    deserialize_from_json,
    serialize_to_json,
    train_meta_labeler,
)


def _example(
    detector="D10b_election_ladder",
    calibrated_p=0.6,
    edge=80.0,
    price=0.4,
    horizon=1800,
    category="Sports",
    volume=50000.0,
    v2=True,
    outcome=1,
):
    return MetaLabelerExample(
        detector_id=detector,
        calibrated_p=calibrated_p,
        edge_pct=edge,
        entry_price=price,
        horizon_sec=horizon,
        category=category,
        volume_24h=volume,
        is_v2_post=v2,
        outcome=outcome,
    )


def _synth_dataset(n=300, seed=42) -> list[MetaLabelerExample]:
    rng = random.Random(seed)
    detectors = list(KNOWN_DETECTORS)
    categories = list(KNOWN_CATEGORIES)
    examples = []
    for _ in range(n):
        det = rng.choice(detectors)
        p = rng.uniform(0.3, 0.85)
        category = rng.choice(categories)
        true_p = p * (1.1 if det == "D10b_election_ladder" else 0.95)
        outcome = 1 if rng.random() < min(0.95, true_p) else 0
        examples.append(
            _example(
                detector=det,
                calibrated_p=p,
                edge=rng.uniform(20.0, 100.0),
                price=rng.uniform(0.2, 0.8),
                horizon=rng.choice([1800, 3600, 21600]),
                category=category,
                volume=rng.uniform(1000, 100000),
                v2=rng.random() > 0.5,
                outcome=outcome,
            )
        )
    return examples


def test_vectorize_shape_matches_feature_names():
    ex = [_example(), _example(detector="D3_cross_mkt_arb")]
    x, y, feature_names = _vectorize(ex)
    assert x.shape == (2, len(feature_names))
    assert y.shape == (2,)
    assert "calibrated_p" in feature_names
    assert "detector_D10b_election_ladder" in feature_names
    assert "detector_D3_cross_mkt_arb" in feature_names


def test_vectorize_one_hot_detector():
    ex = [_example(detector="D10b_election_ladder")]
    x, _, names = _vectorize(ex)
    d1_idx = names.index("detector_D10b_election_ladder")
    d3_idx = names.index("detector_D3_cross_mkt_arb")
    assert x[0, d1_idx] == 1.0
    assert x[0, d3_idx] == 0.0


def test_vectorize_unknown_category_zero_one_hot():
    ex = [_example(category="Unknown_Category")]
    x, _, names = _vectorize(ex)
    cat_indices = [i for i, n in enumerate(names) if n.startswith("category_")]
    assert all(x[0, i] == 0.0 for i in cat_indices)


def test_vectorize_none_category_zero_one_hot():
    ex = [_example(category=None)]
    x, _, names = _vectorize(ex)
    cat_indices = [i for i, n in enumerate(names) if n.startswith("category_")]
    assert all(x[0, i] == 0.0 for i in cat_indices)


def test_vectorize_v2_post_bool_to_float():
    ex = [_example(v2=True), _example(v2=False)]
    x, _, names = _vectorize(ex)
    v2_idx = names.index("is_v2_post")
    assert x[0, v2_idx] == 1.0
    assert x[1, v2_idx] == 0.0


def test_train_too_few_samples_raises():
    examples = [_example() for _ in range(50)]
    with pytest.raises(ValueError, match="min_train_samples"):
        train_meta_labeler(examples, config=MetaLabelerConfig(min_train_samples=100))


def test_train_returns_model_with_metrics():
    dataset = _synth_dataset(n=400)
    model = train_meta_labeler(dataset)
    assert model.n_train > 0
    assert model.n_holdout > 0
    assert 0 <= model.train_brier <= 1
    assert 0 <= model.holdout_brier <= 1
    assert len(model.feature_names) > 5


def test_predict_returns_probabilities_in_unit_interval():
    dataset = _synth_dataset(n=300)
    model = train_meta_labeler(dataset)
    test_examples = [_example() for _ in range(10)]
    preds = model.predict(test_examples)
    assert preds.shape == (10,)
    assert np.all(preds >= 0.0)
    assert np.all(preds <= 1.0)


def test_predict_one_returns_scalar():
    dataset = _synth_dataset(n=300)
    model = train_meta_labeler(dataset)
    p = model.predict_one(_example())
    assert isinstance(p, float)
    assert 0.0 <= p <= 1.0


def test_feature_importance_returns_dict():
    dataset = _synth_dataset(n=300)
    model = train_meta_labeler(dataset)
    fi = model.feature_importance()
    assert isinstance(fi, dict)
    assert "calibrated_p" in fi
    assert all(isinstance(v, int) for v in fi.values())


def test_serialize_roundtrip_preserves_predictions():
    dataset = _synth_dataset(n=300)
    model = train_meta_labeler(dataset)
    test_examples = [_example() for _ in range(20)]
    preds_before = model.predict(test_examples)

    json_payload = serialize_to_json(model)
    restored = deserialize_from_json(json_payload)
    preds_after = restored.predict(test_examples)

    assert np.allclose(preds_before, preds_after, rtol=1e-6)
    assert restored.n_train == model.n_train
    assert restored.holdout_brier == pytest.approx(model.holdout_brier, rel=1e-6)


def test_holdout_split_proportions():
    dataset = _synth_dataset(n=400)
    model = train_meta_labeler(dataset, holdout_fraction=0.20)
    assert model.n_train + model.n_holdout == 400
    assert model.n_holdout == 80


def test_predict_uses_model_schema_not_current(monkeypatch):
    """Backward-compat: после расширения KNOWN_DETECTORS старая модель работает.

    Когда в реальности добавляются новые детекторы (D5/D6) в KNOWN_DETECTORS,
    inference на старой active модели должен НЕ падать с feature mismatch —
    модель vectorize'ит примеры в свою (старую) схему.
    """
    from app.calibration import meta_labeler

    # Train на текущей схеме
    dataset = _synth_dataset(n=300)
    old_model = train_meta_labeler(dataset)
    n_old_features = len(old_model.feature_names)
    old_preds = old_model.predict([_example() for _ in range(5)])

    # Имитируем добавление нового детектора — KNOWN_DETECTORS расширилось.
    # Старая модель должна продолжать работать (vectorize_to_schema подгонит).
    new_detectors = list(meta_labeler.KNOWN_DETECTORS) + ["D99_new_detector"]
    monkeypatch.setattr(meta_labeler, "KNOWN_DETECTORS", tuple(new_detectors))

    new_preds = old_model.predict([_example() for _ in range(5)])

    # Predictions должны быть тождественны — модель использует свою схему
    assert np.allclose(old_preds, new_preds, rtol=1e-9)
    # Модель сохраняет свой исходный feature_names
    assert len(old_model.feature_names) == n_old_features


def test_predict_with_unknown_detector_zero_one_hot():
    """Predict на example с detector_id вне модели — all-zero one-hot, не падает."""
    dataset = _synth_dataset(n=200)
    model = train_meta_labeler(dataset)

    # Example с detector_id которого не было в train
    weird = _example(detector="D_completely_unknown")
    p = model.predict_one(weird)
    assert 0.0 <= p <= 1.0


# --- Tier 3.1 фичи: hours_to_resolution, recent_wr_30d, settle_velocity_hours ---


def test_tier31_features_in_feature_names():
    """3 новые скалярные фичи присутствуют в feature_names."""
    x, _, names = _vectorize([_example()])
    assert "hours_to_resolution" in names
    assert "recent_wr_30d" in names
    assert "settle_velocity_hours" in names


def test_tier31_features_default_none_vectorize_to_nan():
    """Дефолт None → NaN в X (НЕ 0.0 — 0 это валидное значение фичи; LGB различает)."""
    ex = _example()  # хелпер не передаёт новые поля → None
    assert ex.hours_to_resolution is None
    assert ex.recent_wr_30d is None
    assert ex.settle_velocity_hours is None

    x, _, names = _vectorize([ex])
    for feat in ("hours_to_resolution", "recent_wr_30d", "settle_velocity_hours"):
        assert np.isnan(x[0, names.index(feat)]), f"{feat} None должен стать NaN"


def test_tier31_features_values_vectorized():
    """Заданные значения попадают в X на правильные позиции."""
    ex = MetaLabelerExample(
        detector_id="D10b_election_ladder",
        calibrated_p=0.6,
        edge_pct=80.0,
        entry_price=0.4,
        horizon_sec=1800,
        category="Sports",
        volume_24h=50000.0,
        is_v2_post=True,
        outcome=1,
        hours_to_resolution=12.5,
        recent_wr_30d=0.42,
        settle_velocity_hours=6.0,
    )
    x, _, names = _vectorize([ex])
    assert x[0, names.index("hours_to_resolution")] == 12.5
    assert x[0, names.index("recent_wr_30d")] == 0.42
    assert x[0, names.index("settle_velocity_hours")] == 6.0


def test_tier31_zero_distinct_from_none():
    """recent_wr_30d=0.0 (детектор с 0% WR) НЕ путается с None/NaN."""
    ex_zero = MetaLabelerExample(
        detector_id="D10b_election_ladder",
        calibrated_p=0.6,
        edge_pct=80.0,
        entry_price=0.4,
        horizon_sec=1800,
        category="Sports",
        volume_24h=50000.0,
        is_v2_post=True,
        outcome=1,
        recent_wr_30d=0.0,
    )
    x, _, names = _vectorize([ex_zero])
    val = x[0, names.index("recent_wr_30d")]
    assert val == 0.0
    assert not np.isnan(val)


def test_tier31_backward_compat_old_model_ignores_new_features():
    """Модель, обученная БЕЗ новых фич в схеме, предсказывает на новых examples.

    Симулируем старую модель: feature_names без Tier 3.1. predict должен
    работать (новые поля просто не в схеме старой модели)."""
    dataset = _synth_dataset(n=300)
    model = train_meta_labeler(dataset)

    # Урезаем feature_names модели до состояния "до Tier 3.1"
    legacy_names = [
        n for n in model.feature_names if n not in ("hours_to_resolution", "recent_wr_30d", "settle_velocity_hours")
    ]
    model.feature_names = legacy_names
    model.booster  # noqa: B018 — booster обучен на полной схеме, но predict_to_schema урежет

    # Example с заполненными новыми фичами — старая схема их проигнорирует
    ex = MetaLabelerExample(
        detector_id="D10b_election_ladder",
        calibrated_p=0.6,
        edge_pct=80.0,
        entry_price=0.4,
        horizon_sec=1800,
        category="Sports",
        volume_24h=50000.0,
        is_v2_post=True,
        outcome=1,
        hours_to_resolution=12.5,
        recent_wr_30d=0.42,
        settle_velocity_hours=6.0,
    )
    # Не должно падать (vectorize_to_schema строит X только по legacy_names)
    from app.calibration.meta_labeler import _vectorize_to_schema

    x = _vectorize_to_schema([ex], legacy_names)
    assert x.shape == (1, len(legacy_names))
