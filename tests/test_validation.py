"""Тесты ML validation harness."""

from __future__ import annotations

import numpy as np
import pytest

from app.calibration.validation import (
    MIN_BAND_SIZE,
    MIN_PER_BAND_AUC,
    auc_score,
    brier_decomposition,
    brier_skill_score,
    calibration_intercept_slope,
    event_cluster_bootstrap_roi,
    expected_calibration_error,
    segment_metrics,
    validate_model,
)

# ── auc_score ────────────────────────────────────────────────────────────────


class TestAucScore:
    def test_perfect_classifier(self):
        y = [0, 0, 0, 1, 1, 1]
        p = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
        assert auc_score(y, p) == pytest.approx(1.0, abs=1e-9)

    def test_random_classifier(self):
        rng = np.random.default_rng(42)
        y = rng.integers(0, 2, size=1000)
        p = rng.uniform(0, 1, size=1000)
        auc = auc_score(y.tolist(), p.tolist())
        assert 0.45 < auc < 0.55

    def test_anti_classifier(self):
        y = [0, 0, 0, 1, 1, 1]
        p = [0.9, 0.8, 0.7, 0.3, 0.2, 0.1]
        assert auc_score(y, p) == pytest.approx(0.0, abs=1e-9)

    def test_returns_none_for_one_class(self):
        assert auc_score([0, 0, 0], [0.1, 0.2, 0.3]) is None
        assert auc_score([1, 1, 1], [0.1, 0.2, 0.3]) is None

    def test_handles_ties(self):
        y = [0, 0, 1, 1]
        p = [0.5, 0.5, 0.5, 0.5]
        assert auc_score(y, p) == pytest.approx(0.5, abs=1e-9)


# ── ECE ──────────────────────────────────────────────────────────────────────


class TestECE:
    def test_perfect_calibration_zero(self):
        # Все predictions = base rate → perfect bin-level calibration
        y = [0] * 50 + [1] * 50
        p = [0.5] * 100
        ece = expected_calibration_error(y, p, n_bins=10)
        assert ece == pytest.approx(0.0, abs=1e-9)

    def test_severe_miscalibration(self):
        # Все predict 0.9 но WR = 0.1 → ECE = 0.8
        y = [0] * 90 + [1] * 10
        p = [0.9] * 100
        ece = expected_calibration_error(y, p, n_bins=10)
        assert ece == pytest.approx(0.8, abs=0.05)

    def test_empty_input(self):
        assert expected_calibration_error([], []) == 0.0


# ── Brier decomposition ─────────────────────────────────────────────────────


class TestBrierDecomposition:
    def test_perfect_predictor(self):
        y = [0, 0, 1, 1]
        p = [0.0, 0.0, 1.0, 1.0]
        d = brier_decomposition(y, p, n_bins=10)
        assert d.brier_score == pytest.approx(0.0, abs=1e-9)
        assert d.reliability == pytest.approx(0.0, abs=1e-9)

    def test_constant_predictor(self):
        # P=0.5 для всех, base_rate=0.5 → reliability=0, resolution=0
        y = [0, 1] * 50
        p = [0.5] * 100
        d = brier_decomposition(y, p, n_bins=10)
        assert d.uncertainty == pytest.approx(0.25, abs=1e-3)
        assert d.reliability == pytest.approx(0.0, abs=1e-3)
        assert d.resolution == pytest.approx(0.0, abs=1e-3)


# ── BSS ──────────────────────────────────────────────────────────────────────


class TestBrierSkillScore:
    def test_better_than_baseline(self):
        y = [0, 0, 1, 1]
        p = [0.1, 0.2, 0.8, 0.9]  # close to truth
        bp = [0.5, 0.5, 0.5, 0.5]  # constant baseline
        bss = brier_skill_score(y, p, bp)
        assert bss > 0.5

    def test_worse_than_baseline(self):
        y = [0, 0, 1, 1]
        p = [0.9, 0.9, 0.1, 0.1]  # anti-baseline
        bp = [0.5, 0.5, 0.5, 0.5]
        bss = brier_skill_score(y, p, bp)
        assert bss < 0


# ── Calibration slope ───────────────────────────────────────────────────────


class TestCalibrationSlope:
    def test_perfect_calibration(self):
        # На больших n с perfect probabilities получим slope ≈ 1, intercept ≈ 0
        rng = np.random.default_rng(1)
        n = 10_000
        p = rng.uniform(0.05, 0.95, size=n)
        y = rng.binomial(1, p)
        intercept, slope = calibration_intercept_slope(y.tolist(), p.tolist())
        assert slope == pytest.approx(1.0, abs=0.10)
        assert abs(intercept) < 0.10


# ── Per-segment ─────────────────────────────────────────────────────────────


class TestSegmentMetrics:
    def test_segments_partitioned(self):
        rng = np.random.default_rng(2)
        n = 1000
        seg = rng.uniform(0, 1, size=n)
        p = rng.uniform(0, 1, size=n)
        y = rng.binomial(1, p)
        segs = segment_metrics(y.tolist(), p.tolist(), seg.tolist())
        # Sum of n across segments = total
        assert sum(s.n for s in segs) == n


# ── Bootstrap ───────────────────────────────────────────────────────────────


class TestBootstrap:
    def test_zero_roi_ci_includes_zero(self):
        rng = np.random.default_rng(3)
        events = [f"e{i // 5}" for i in range(500)]
        roi = rng.normal(0.0, 0.1, size=500).tolist()
        lo, hi = event_cluster_bootstrap_roi(events, roi, n_resamples=500, seed=3)
        # Симметрично около 0 → CI включает 0
        assert lo < 0 < hi

    def test_positive_roi_ci_excludes_zero(self):
        rng = np.random.default_rng(4)
        events = [f"e{i // 3}" for i in range(900)]
        # Strong positive signal — выскакиваем из noise
        roi = rng.normal(0.5, 0.1, size=900).tolist()
        lo, hi = event_cluster_bootstrap_roi(events, roi, n_resamples=500, seed=4)
        assert lo > 0
        assert hi > lo


# ── Full validation ─────────────────────────────────────────────────────────


class TestValidateModel:
    def test_d_mlcalibrator_style_fails_gate(self):
        """Симулируем D-MLCalibrator: global AUC хороший, per-band random."""
        rng = np.random.default_rng(5)
        n = 5000
        # 80% rows в extreme zones (yes_price > 0.9 или < 0.1) — там AUC высокий
        # 20% rows в [0.4, 0.6] mid-zone — там AUC ~ 0.5 (random)
        is_extreme = rng.uniform(size=n) < 0.8
        prices = np.where(
            is_extreme,
            np.where(rng.uniform(size=n) < 0.5, rng.uniform(0.0, 0.10, n), rng.uniform(0.90, 1.0, n)),
            rng.uniform(0.40, 0.60, n),
        )
        # Outcomes: extreme zones — predictable (yes_price ≈ true_p), mid — random
        true_p = np.where(is_extreme, prices, 0.5)
        y = rng.binomial(1, true_p)
        # Predictions: extreme — close to truth, mid — random
        p_extreme = true_p + rng.normal(0, 0.05, n)
        p_mid = rng.uniform(0, 1, n)
        preds = np.clip(np.where(is_extreme, p_extreme, p_mid), 0.001, 0.999)

        report = validate_model(
            name="D-MLCalibrator-mock",
            y_true=y.tolist(),
            y_pred=preds.tolist(),
            market_price=prices.tolist(),
            segment_value=prices.tolist(),
            event_ids=[f"e{i // 3}" for i in range(n)],
            roi_per_row=[0.0] * n,
        )

        # Global AUC должен быть высоким (extreme dominates)
        assert report.overall_auc is not None
        assert report.overall_auc > 0.85

        # НО per-band [0.40-0.60) AUC должен быть ниже MIN_PER_BAND_AUC
        # Поэтому gate должен FAIL
        mid_band = [s for s in report.per_segment if s.segment.startswith("[0.50") or s.segment.startswith("[0.30")]
        # Хоть один из mid-bands с n>=100 должен иметь AUC < threshold
        any_failing = any(
            s.n >= 100 and s.auc is not None and s.auc < MIN_PER_BAND_AUC
            for s in mid_band
        )
        assert any_failing, "Expected at least one mid-band with random AUC"

        # Gate failures должен содержать per-band AUC complaint
        assert not report.passes_gate
        assert any("per-band AUC" in f for f in report.failures)

    def test_well_calibrated_no_edge_fails_bss_gate(self):
        """Хорошо откалиброванная модель БЕЗ edge vs market — BSS gate FAILS.
        Это правильное поведение: модель не лучше market price → не promote."""
        rng = np.random.default_rng(6)
        n = 5000
        # Linearly distributed prices across full range
        prices = rng.uniform(0.05, 0.95, size=n)
        y = rng.binomial(1, prices)
        # Model predicts true probabilities + small noise
        preds = np.clip(prices + rng.normal(0, 0.02, n), 0.01, 0.99)

        report = validate_model(
            name="well-calibrated-no-edge",
            y_true=y.tolist(),
            y_pred=preds.tolist(),
            market_price=prices.tolist(),  # market = true probability
            segment_value=prices.tolist(),
            event_ids=[f"e{i // 3}" for i in range(n)],
            roi_per_row=[0.0] * n,
        )

        # BSS vs same baseline ≈ 0 → fails gate (правильно — нет улучшения)
        assert report.brier_skill_score < MIN_BAND_SIZE * 0 + 0.05  # < 0.05
        # Calibration slope близко к 1.0 (модель калиброванная)
        assert abs(report.calibration_slope - 1.0) < 0.15
        # Gate fails хотя бы из-за BSS
        assert not report.passes_gate
        assert any("Brier Skill Score" in f for f in report.failures)

    def test_truly_better_than_market_passes_gate(self):
        """Модель ЛУЧШЕ market (BSS > 0.05) проходит gate."""
        rng = np.random.default_rng(7)
        n = 5000
        # True probabilities — НЕ те же что market price
        true_p = rng.uniform(0.05, 0.95, size=n)
        # Market price — noisy estimate
        market_p = np.clip(true_p + rng.normal(0, 0.20, n), 0.01, 0.99)
        y = rng.binomial(1, true_p)
        # Model — точно знает true_p (overlay для теста — но это сценарий "edge")
        preds = np.clip(true_p + rng.normal(0, 0.03, n), 0.01, 0.99)
        # ROI per row: positive (we have edge)
        roi = (y - market_p) / market_p
        # Cap чтобы избежать division-by-near-zero blow-up
        roi = np.clip(roi, -1.0, 5.0)

        report = validate_model(
            name="model-with-edge",
            y_true=y.tolist(),
            y_pred=preds.tolist(),
            market_price=market_p.tolist(),
            segment_value=market_p.tolist(),
            event_ids=[f"e{i // 5}" for i in range(n)],
            roi_per_row=roi.tolist(),
        )

        # Должна быть positive BSS
        assert report.brier_skill_score > 0.05
        # Bootstrap CI lower bound > 0 (positive ROI)
        assert report.bootstrap_roi_ci is not None
        assert report.bootstrap_roi_ci[0] > 0

    def test_bootstrap_ci_deterministic_across_calls(self):
        """validate_model с одинаковым входом → идентичный bootstrap_roi_ci.

        Регрессия: раньше event_cluster_bootstrap_roi вызывался без seed →
        нижняя граница CI гуляла вокруг нуля между прогонами и флейкала CI
        (assert ci[0] > 0 падал на -0.0002). Дефолтный seed делает CI
        воспроизводимым при одинаковых данных.
        """
        rng = np.random.default_rng(11)
        n = 2000
        true_p = rng.uniform(0.05, 0.95, size=n)
        market_p = np.clip(true_p + rng.normal(0, 0.20, n), 0.01, 0.99)
        y = rng.binomial(1, true_p)
        preds = np.clip(true_p + rng.normal(0, 0.03, n), 0.01, 0.99)
        roi = np.clip((y - market_p) / market_p, -1.0, 5.0)
        kwargs = dict(
            name="determinism",
            y_true=y.tolist(),
            y_pred=preds.tolist(),
            market_price=market_p.tolist(),
            segment_value=market_p.tolist(),
            event_ids=[f"e{i // 5}" for i in range(n)],
            roi_per_row=roi.tolist(),
        )
        ci_a = validate_model(**kwargs).bootstrap_roi_ci
        ci_b = validate_model(**kwargs).bootstrap_roi_ci
        assert ci_a == ci_b
