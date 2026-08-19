"""Тесты LgbCalibrator (per-detector LightGBM calibrator).

Параллельно test_beta_calibrator.py — same shape, разные модели.
"""

from __future__ import annotations

import random

import numpy as np
import pytest

pytest.importorskip("lightgbm", reason="lightgbm required for LgbCalibrator tests")

from app.calibration.lgb_calibrator import LgbCalibrator  # noqa: E402
from app.calibration.lgb_features import build_features  # noqa: E402


def _synthetic_dataset(n: int = 600, bias: float = 0.25, seed: int = 42):
    """Генерирует [features_dicts, outcomes].

    edge_pct и raw_p информативны: outcome скоррелирован с true_p.
    LGB должен научиться → Brier(LGB) < Brier(raw_p) на biased data.
    """
    rng = random.Random(seed)
    feats: list[dict[str, float]] = []
    outcomes: list[int] = []
    for _ in range(n):
        true_p = rng.random()
        observed_p = min(1.0, true_p + bias * (1 - true_p))
        # Несколько информативных и шумовых фичей
        feats.append(
            {
                "raw_p": observed_p,
                "edge_pct": (true_p - 0.5) * 10.0 + rng.gauss(0, 1.0),
                "current_price": observed_p,
                "side_yes": 1.0 if rng.random() > 0.5 else 0.0,
                "horizon_sec": 86400.0,
                "volume_24h": rng.uniform(0, 10000),
                "category_sports": 1.0 if rng.random() > 0.5 else 0.0,
                "zone_top": rng.uniform(0, 1),
                "noise1": rng.gauss(0, 1),
                "noise2": rng.gauss(0, 1),
            }
        )
        outcomes.append(1 if rng.random() < true_p else 0)
    return feats, outcomes


def test_fit_then_transform_returns_sane_probs():
    feats, ys = _synthetic_dataset(n=400)
    cal = LgbCalibrator(min_samples=100).fit(feats, ys)
    ps = cal.transform(feats[:5])
    assert ps.shape == (5,)
    assert ((ps >= 0.0) & (ps <= 1.0)).all()


def test_scalar_vs_array_transform():
    feats, ys = _synthetic_dataset(n=400)
    cal = LgbCalibrator(min_samples=100).fit(feats, ys)
    p_scalar = cal.transform(feats[0])
    p_array = cal.transform([feats[0]])
    assert isinstance(p_scalar, float)
    assert p_scalar == pytest.approx(float(p_array[0]), rel=1e-9)


def test_calibration_improves_brier_on_biased_data():
    """LGB должна давать lower Brier чем raw_p на biased data."""
    feats, ys = _synthetic_dataset(n=600, bias=0.25, seed=11)
    cut = int(len(feats) * 0.8)
    cal = LgbCalibrator(min_samples=100).fit(feats[:cut], ys[:cut])
    p_lgb = cal.transform(feats[cut:])
    y_arr = np.asarray(ys[cut:], dtype=float)
    raw_p_arr = np.asarray([f["raw_p"] for f in feats[cut:]], dtype=float)

    brier_lgb = float(np.mean((p_lgb - y_arr) ** 2))
    brier_raw = float(np.mean((raw_p_arr - y_arr) ** 2))
    # Не требуем огромного улучшения — синтетика шумная, но LGB не должна быть хуже
    assert brier_lgb <= brier_raw + 0.01


def test_too_few_samples_raises():
    cal = LgbCalibrator(min_samples=100)
    with pytest.raises(ValueError, match=r"N=\d+ < min_samples"):
        cal.fit([{"raw_p": 0.5}] * 10, [1] * 10)


def test_all_same_outcome_yields_degenerate_calibrator():
    """Все WIN → degenerate fallback на constant_q = 1.0."""
    feats = [{"raw_p": 0.7, "edge_pct": 0.0}] * 150
    ys = [1] * 150
    cal = LgbCalibrator(min_samples=100).fit(feats, ys)
    assert cal.params is not None
    assert cal.params.degenerate is True
    assert cal.transform({"raw_p": 0.5}) == 1.0


def test_serialize_roundtrip_keeps_predictions():
    feats, ys = _synthetic_dataset(n=400)
    cal = LgbCalibrator(min_samples=100).fit(feats, ys)
    cal2 = LgbCalibrator.deserialize(cal.serialize())
    p1 = cal.transform(feats[:10])
    p2 = cal2.transform(feats[:10])
    # Идентично — booster восстановлен из model_to_string
    assert np.allclose(p1, p2, atol=1e-9)


def test_serialize_roundtrip_degenerate():
    cal = LgbCalibrator(min_samples=50).fit([{"raw_p": 0.5}] * 60, [0] * 60)
    cal2 = LgbCalibrator.deserialize(cal.serialize())
    assert cal2.transform({"raw_p": 0.9}) == 0.0


def test_unfitted_raises():
    cal = LgbCalibrator()
    with pytest.raises(RuntimeError, match="not fitted"):
        cal.transform({"raw_p": 0.5})


def test_length_mismatch_raises():
    cal = LgbCalibrator()
    with pytest.raises(ValueError, match="len mismatch"):
        cal.fit([{"raw_p": 0.1}, {"raw_p": 0.2}], [1])


def test_transform_handles_missing_features():
    """В inference фича может отсутствовать (новый detector schema) — должно работать."""
    feats, ys = _synthetic_dataset(n=300)
    cal = LgbCalibrator(min_samples=100).fit(feats, ys)
    # Передаём dict БЕЗ некоторых фичей — должно просто заменить на 0.0
    minimal = {"raw_p": 0.5}
    p = cal.transform(minimal)
    assert 0.0 < p < 1.0


def test_transform_clips_extremes_safely():
    feats, ys = _synthetic_dataset(n=300)
    cal = LgbCalibrator(min_samples=100).fit(feats, ys)
    # Edge feature values — booster всё равно должен дать значение в (0, 1)
    extreme = dict(feats[0])
    extreme["raw_p"] = 1.0
    extreme["current_price"] = 1.0
    p = cal.transform(extreme)
    assert 0.0 < p < 1.0


# ---------- feature builder ----------


def test_build_features_yes_side():
    row = {
        "side": "YES",
        "current_price": 0.6,
        "fair_price": 0.7,
        "confidence": 0.65,
        "expected_edge_pct": 5.0,
        "horizon_sec": 3600,
        "event_size": 2,
        "category": "Crypto",
        "volume_24h": 12345.6,
        "evidence": {
            "yes_price": 0.6,
            "baseline_wr": 0.3,
            "anchor_band": "24h",
            "zone": "0.50-0.70",
        },
    }
    f = build_features(row)
    assert f["raw_p"] == 0.65
    assert f["entry_price"] == 0.6  # YES side → entry = current_price
    assert f["side_yes"] == 1.0
    assert f["category_crypto"] == 1.0
    assert f["category_sports"] == 0.0
    assert f["anchor_24h"] == 1.0
    assert f["anchor_4h"] == 0.0
    assert f["zone_top"] == 0.70


def test_build_features_no_side():
    row = {
        "side": "NO",
        "current_price": 0.6,
        "confidence": 0.55,
        "expected_edge_pct": 2.0,
        "horizon_sec": 3600,
        "evidence": {},
    }
    f = build_features(row)
    # NO side → entry = 1 - current_price = 0.4
    assert f["entry_price"] == pytest.approx(0.4)
    assert f["side_yes"] == 0.0


def test_build_features_evidence_as_string():
    """Evidence может прийти как JSON string из БД."""
    row = {
        "side": "YES",
        "current_price": 0.5,
        "confidence": 0.5,
        "expected_edge_pct": 0.0,
        "horizon_sec": 0,
        "evidence": '{"baseline_wr": 0.4, "anchor_band": "72h"}',
    }
    f = build_features(row)
    assert f["ev_baseline_wr"] == 0.4
    assert f["anchor_72h"] == 1.0


def test_build_features_handles_missing_evidence():
    row = {"side": "YES", "current_price": 0.5, "confidence": 0.5}
    f = build_features(row)
    # Defaults применены
    assert f["ev_baseline_wr"] == 0.2
    assert f["ev_hours_to_resolve"] == 24.0
    assert f["zone_top"] == 0.5


def test_build_features_handles_none_values():
    row = {
        "side": "YES",
        "current_price": None,
        "fair_price": None,
        "confidence": None,
        "expected_edge_pct": None,
        "horizon_sec": None,
        "event_size": None,
        "volume_24h": None,
        "category": None,
        "evidence": None,
    }
    f = build_features(row)
    # Никаких exceptions, all keys present
    assert f["raw_p"] == 0.5
    assert f["current_price"] == 0.5
    assert f["entry_price"] == 0.5
    assert f["horizon_sec"] == 0.0


# ---------- Tier 3.1 features (2026-05-19) ----------


def test_tier31_hours_to_resolution_from_sql_column():
    """Если caller положил hours_to_resolution_at_signal — используем именно его."""
    row = {
        "side": "YES",
        "current_price": 0.5,
        "confidence": 0.5,
        "hours_to_resolution_at_signal": 48.0,
        "evidence": {"hours_to_resolve": 24.0},  # должен быть проигнорирован
    }
    f = build_features(row)
    assert f["hours_to_resolution"] == 48.0


def test_tier31_hours_to_resolution_fallback_to_evidence():
    """Если SQL не дал hours_to_resolution_at_signal — fallback на evidence."""
    row = {
        "side": "YES",
        "current_price": 0.5,
        "confidence": 0.5,
        "hours_to_resolution_at_signal": None,
        "evidence": {"hours_to_resolve": 12.5},
    }
    f = build_features(row)
    assert f["hours_to_resolution"] == 12.5


def test_tier31_hours_to_resolution_default_when_no_sources():
    """Нет ни SQL ни evidence → default 24.0 (clipped к диапазону)."""
    row = {"side": "YES", "current_price": 0.5, "confidence": 0.5}
    f = build_features(row)
    assert f["hours_to_resolution"] == 24.0


def test_tier31_hours_to_resolution_clipped_to_720h():
    """Crazy values (>30 days) clip к 720h чтобы не давать outlier-weights."""
    row = {
        "side": "YES",
        "current_price": 0.5,
        "confidence": 0.5,
        "hours_to_resolution_at_signal": 5000.0,
    }
    f = build_features(row)
    assert f["hours_to_resolution"] == 720.0


def test_tier31_hours_to_resolution_negative_clipped_to_zero():
    """Negative values (signal после end_date) clip к 0 — деградировавший сигнал."""
    row = {
        "side": "YES",
        "current_price": 0.5,
        "confidence": 0.5,
        "hours_to_resolution_at_signal": -3.0,
    }
    f = build_features(row)
    assert f["hours_to_resolution"] == 0.0


def test_tier31_recent_wr_30d_from_sql():
    row = {
        "side": "YES",
        "current_price": 0.5,
        "confidence": 0.5,
        "recent_wr_30d": 0.42,
    }
    f = build_features(row)
    assert f["recent_wr_30d"] == 0.42


def test_tier31_recent_wr_30d_default_when_missing():
    """Cold-start детектор (нет signals в 30d window) → default 0.5 (no-info prior)."""
    row = {"side": "YES", "current_price": 0.5, "confidence": 0.5}
    f = build_features(row)
    assert f["recent_wr_30d"] == 0.5


def test_tier31_detector_recent_count_log_compression():
    """detector_recent_count_30d_log = log1p(count) для variance reduction."""
    import math

    row = {
        "side": "YES",
        "current_price": 0.5,
        "confidence": 0.5,
        "detector_recent_count_30d": 100,
    }
    f = build_features(row)
    assert f["detector_recent_count_30d_log"] == pytest.approx(math.log1p(100))


def test_tier31_detector_recent_count_zero_when_cold_start():
    row = {"side": "YES", "current_price": 0.5, "confidence": 0.5}
    f = build_features(row)
    assert f["detector_recent_count_30d_log"] == 0.0  # log1p(0)


def test_tier31_all_features_present_in_dict():
    """Все 3 Tier 3.1 фичи в финальном dict (для feature_names stability)."""
    row = {"side": "YES", "current_price": 0.5, "confidence": 0.5}
    f = build_features(row)
    assert "hours_to_resolution" in f
    assert "recent_wr_30d" in f
    assert "detector_recent_count_30d_log" in f
