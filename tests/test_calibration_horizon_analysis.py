"""Тесты для calibration_horizon_analysis (Phase 2.A).

Проверяет:
- assign_horizon_bin корректно мапит float days → bin label
- brier_score математика правильная
- analyse_cell скипает малые subsets, возвращает metrics для больших
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "research"))

from calibration_horizon_analysis import (  # noqa: E402
    HORIZON_BINS,
    MIN_SAMPLES_PER_BIN,
    analyse_cell,
    assign_horizon_bin,
    brier_score,
)


class TestAssignHorizonBin:
    def test_short_horizon(self):
        assert assign_horizon_bin(0.5) == "<3d"
        assert assign_horizon_bin(2.99) == "<3d"

    def test_3_to_7_horizon(self):
        assert assign_horizon_bin(3.0) == "3-7d"
        assert assign_horizon_bin(6.99) == "3-7d"

    def test_7_to_14_horizon(self):
        assert assign_horizon_bin(7.0) == "7-14d"
        assert assign_horizon_bin(13.99) == "7-14d"

    def test_14_to_30_horizon(self):
        assert assign_horizon_bin(14.0) == "14-30d"
        assert assign_horizon_bin(29.99) == "14-30d"

    def test_long_horizon(self):
        assert assign_horizon_bin(30.0) == ">30d"
        assert assign_horizon_bin(365.0) == ">30d"

    def test_outlier_horizon(self):
        # > 1000d → None (outliers исключаются)
        assert assign_horizon_bin(1500.0) is None


class TestBrierScore:
    def test_perfect_predictions(self):
        # All predictions exactly correct
        preds = np.array([1.0, 0.0, 1.0, 0.0])
        outcomes = np.array([1, 0, 1, 0])
        assert brier_score(preds, outcomes) == 0.0

    def test_worst_predictions(self):
        # All predictions exactly wrong
        preds = np.array([0.0, 1.0, 0.0, 1.0])
        outcomes = np.array([1, 0, 1, 0])
        assert brier_score(preds, outcomes) == 1.0

    def test_random_predictions(self):
        # 50/50 probabilities → 0.25 (max possible Brier for binary)
        preds = np.array([0.5, 0.5, 0.5, 0.5])
        outcomes = np.array([1, 0, 1, 0])
        assert brier_score(preds, outcomes) == 0.25


class TestAnalyseCell:
    @pytest.fixture
    def small_cell(self):
        """50 rows — ниже MIN_SAMPLES_PER_BIN (200)."""
        return pd.DataFrame({
            "oracle_prod": np.random.uniform(0.3, 0.7, 50),
            "winner_is_yes": np.random.randint(0, 2, 50),
            "prod_win": np.random.randint(0, 2, 50),
        })

    @pytest.fixture
    def large_cell(self):
        """500 rows — выше MIN_SAMPLES_PER_BIN."""
        np.random.seed(42)
        n = 500
        return pd.DataFrame({
            "oracle_prod": np.random.uniform(0.3, 0.7, n),
            "winner_is_yes": np.random.randint(0, 2, n),
            "prod_win": np.random.randint(0, 2, n),
        })

    def test_skip_small_cell(self, small_cell):
        result = analyse_cell(small_cell, n_optuna_trials=5)
        assert "skip_reason" in result
        assert f"n<{MIN_SAMPLES_PER_BIN}" in result["skip_reason"]

    def test_analyse_large_cell_returns_metrics(self, large_cell):
        result = analyse_cell(large_cell, n_optuna_trials=5)
        assert "skip_reason" not in result
        assert "brier_default" in result
        assert "brier_optimized" in result
        assert "improvement_pct" in result
        assert "best_a" in result
        assert "best_b" in result
        assert "is_hot" in result

    def test_analyse_brier_in_valid_range(self, large_cell):
        """Brier должен быть в [0, 1] для probabilities в [0, 1] и outcomes 0/1."""
        result = analyse_cell(large_cell, n_optuna_trials=5)
        assert 0.0 <= result["brier_default"] <= 1.0
        assert 0.0 <= result["brier_optimized"] <= 1.0

    def test_optimization_does_not_make_brier_worse(self, large_cell):
        """Optuna не должна делать хуже чем default — она минимизирует Brier."""
        result = analyse_cell(large_cell, n_optuna_trials=10)
        assert result["brier_optimized"] <= result["brier_default"] + 1e-6


class TestHorizonBinsCoverage:
    def test_no_gaps_in_bins(self):
        """Bins покрывают [0, 1000) без gaps."""
        bins_sorted = sorted(HORIZON_BINS, key=lambda x: x[1])
        prev_hi = 0
        for label, lo, hi in bins_sorted:
            assert lo == prev_hi, f"Gap before {label}: prev_hi={prev_hi}, lo={lo}"
            assert lo < hi, f"{label} has lo>=hi"
            prev_hi = hi
