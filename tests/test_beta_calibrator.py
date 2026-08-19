"""Тесты BetaCalibrator (Kull 2017)."""

from __future__ import annotations

import random

import numpy as np
import pytest

from app.calibration.beta_calibrator import BetaCalibrator, raw_brier


def _synthetic_dataset(n: int = 500, bias: float = 0.0, seed: int = 42):
    """Генерирует raw_p и outcomes так, чтобы было overconfidence (bias > 0).

    bias = 0  → calibrated, raw_p = true probability
    bias > 0  → детектор переоценивает (raw_p inflated), нужна Beta cal
    """
    rng = random.Random(seed)
    raw_p = []
    outcome = []
    for _ in range(n):
        true_p = rng.random()
        observed_p = min(1.0, true_p + bias * (1 - true_p))
        raw_p.append(observed_p)
        outcome.append(1 if rng.random() < true_p else 0)
    return raw_p, outcome


def test_fit_then_transform_returns_sane_probs():
    raw_p, outcome = _synthetic_dataset(n=200, bias=0.2)
    cal = BetaCalibrator(min_samples=30).fit(raw_p, outcome)
    qs = cal.transform([0.1, 0.5, 0.9])
    assert all(0.0 <= q <= 1.0 for q in qs)


def test_calibration_improves_brier_on_biased_data():
    raw_p, outcome = _synthetic_dataset(n=500, bias=0.25)
    cal = BetaCalibrator(min_samples=30).fit(raw_p, outcome)
    brier_before = raw_brier(raw_p, outcome)
    brier_after = cal.brier(raw_p, outcome)
    assert brier_after < brier_before


def test_too_few_samples_raises():
    cal = BetaCalibrator(min_samples=30)
    with pytest.raises(ValueError, match="N=10"):
        cal.fit([0.5] * 10, [1] * 10)


def test_all_same_outcome_yields_degenerate_calibrator():
    """Если все WIN — Beta cal не может fit, fallback на constant."""
    raw_p = [0.7] * 100
    outcome = [1] * 100
    cal = BetaCalibrator(min_samples=30).fit(raw_p, outcome)
    assert cal.params is not None
    assert cal.params.degenerate is True
    assert cal.transform(0.5) == 1.0


def test_serialize_roundtrip():
    raw_p, outcome = _synthetic_dataset(n=100, bias=0.1)
    cal = BetaCalibrator(min_samples=30).fit(raw_p, outcome)
    data = cal.serialize()
    cal2 = BetaCalibrator.deserialize(data)
    a = cal.transform([0.3, 0.5, 0.7])
    b = cal2.transform([0.3, 0.5, 0.7])
    assert np.allclose(a, b, rtol=1e-9)


def test_serialize_roundtrip_degenerate():
    cal = BetaCalibrator(min_samples=10).fit([0.5] * 30, [0] * 30)
    cal2 = BetaCalibrator.deserialize(cal.serialize())
    assert cal2.transform(0.7) == 0.0


def test_transform_clips_extremes_safely():
    raw_p, outcome = _synthetic_dataset(n=100, bias=0.0)
    cal = BetaCalibrator(min_samples=30).fit(raw_p, outcome)
    q0 = cal.transform(0.0)
    q1 = cal.transform(1.0)
    assert 0.0 < q0 < 1.0
    assert 0.0 < q1 < 1.0


def test_unfitted_raises():
    cal = BetaCalibrator()
    with pytest.raises(RuntimeError, match="not fitted"):
        cal.transform(0.5)


def test_length_mismatch_raises():
    cal = BetaCalibrator()
    with pytest.raises(ValueError, match="len mismatch"):
        cal.fit([0.1, 0.2, 0.3], [1, 0])


def test_scalar_vs_array_transform():
    raw_p, outcome = _synthetic_dataset(n=100, bias=0.0)
    cal = BetaCalibrator(min_samples=30).fit(raw_p, outcome)
    q_scalar = cal.transform(0.5)
    q_array = cal.transform([0.5])
    assert isinstance(q_scalar, float)
    assert q_scalar == pytest.approx(q_array[0], rel=1e-9)
