"""Тесты для pit_v2_vs_v3_diff.py (Phase 1.C)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "research"))

from pit_v2_vs_v3_diff import (  # noqa: E402
    DIFF_THRESHOLD_PCT,
    compare_categories,
    compare_columns,
    compare_phases,
    safe_diff_pct,
)


class TestSafeDiffPct:
    def test_normal_diff(self):
        # 10 → 11 = +10%
        assert safe_diff_pct(10.0, 11.0) == pytest.approx(10.0)

    def test_zero_change(self):
        assert safe_diff_pct(5.0, 5.0) == pytest.approx(0.0)

    def test_zero_v2_zero_v3(self):
        # Both zero → 0% (no drift)
        assert safe_diff_pct(0.0, 0.0) == 0.0

    def test_zero_v2_nonzero_v3(self):
        # Drift from zero — undefined, return inf
        assert safe_diff_pct(0.0, 0.1) == float("inf")

    def test_negative_decrease(self):
        # 10 → 5 = -50%
        assert safe_diff_pct(10.0, 5.0) == pytest.approx(-50.0)


class TestCompareColumns:
    @pytest.fixture
    def df_v2(self):
        np.random.seed(42)
        return pd.DataFrame({
            "market_price": np.full(100, 0.5),
            "whale_raw": np.full(100, 0.0),
            "maker_raw": np.full(100, 0.1),
            "taker_raw": np.full(100, 0.0),
            "oracle_prod": np.full(100, 0.6),
            "oracle_research": np.full(100, 0.4),
            "div_prod": np.full(100, 0.1),
            "div_research": np.full(100, -0.1),
            "prod_win": np.full(100, 0.6),
            "research_win": np.full(100, 0.4),
            "winner_is_yes": np.full(100, 0.5),
            "total_volume_usd": np.full(100, 50000.0),
            "horizon_days": np.full(100, 7.0),
        })

    @pytest.fixture
    def df_v3_no_drift(self, df_v2):
        # Identical to v2 — diff should be 0%
        return df_v2.copy()

    @pytest.fixture
    def df_v3_minor_drift(self):
        # +3% mean change — within ±5% threshold
        return pd.DataFrame({
            "market_price": np.full(1000, 0.515),  # +3%
            "whale_raw": np.full(1000, 0.0),
            "maker_raw": np.full(1000, 0.103),
            "taker_raw": np.full(1000, 0.0),
            "oracle_prod": np.full(1000, 0.618),  # +3%
            "oracle_research": np.full(1000, 0.412),
            "div_prod": np.full(1000, 0.103),
            "div_research": np.full(1000, -0.103),
            "prod_win": np.full(1000, 0.618),
            "research_win": np.full(1000, 0.412),
            "winner_is_yes": np.full(1000, 0.515),
            "total_volume_usd": np.full(1000, 51500.0),
            "horizon_days": np.full(1000, 7.21),
        })

    @pytest.fixture
    def df_v3_major_drift(self):
        # +20% — outside ±5%
        return pd.DataFrame({
            "market_price": np.full(1000, 0.6),  # +20%
            "whale_raw": np.full(1000, 0.0),
            "maker_raw": np.full(1000, 0.12),
            "taker_raw": np.full(1000, 0.0),
            "oracle_prod": np.full(1000, 0.72),
            "oracle_research": np.full(1000, 0.48),
            "div_prod": np.full(1000, 0.12),
            "div_research": np.full(1000, -0.12),
            "prod_win": np.full(1000, 0.72),
            "research_win": np.full(1000, 0.48),
            "winner_is_yes": np.full(1000, 0.6),
            "total_volume_usd": np.full(1000, 60000.0),
            "horizon_days": np.full(1000, 8.4),
        })

    def test_no_drift_all_in_range(self, df_v2, df_v3_no_drift):
        results = compare_columns(df_v2, df_v3_no_drift)
        for r in results:
            if "skip" not in r:
                assert r["in_range"], f"{r['column']} should be in range"

    def test_minor_drift_in_range(self, df_v2, df_v3_minor_drift):
        results = compare_columns(df_v2, df_v3_minor_drift)
        # market_price: 0.5 → 0.515 = +3% (in range, threshold ±5%)
        mp_result = next(r for r in results if r["column"] == "market_price")
        assert mp_result["in_range"]

    def test_major_drift_out_of_range(self, df_v2, df_v3_major_drift):
        results = compare_columns(df_v2, df_v3_major_drift)
        # market_price: 0.5 → 0.6 = +20% (out of range)
        mp_result = next(r for r in results if r["column"] == "market_price")
        assert not mp_result["in_range"]
        assert abs(mp_result["diff_pct"]) > DIFF_THRESHOLD_PCT


class TestCompareCategories:
    def test_categories_identical(self):
        df = pd.DataFrame({"category": ["A", "B", "C"] * 10})
        result = compare_categories(df, df)
        assert result["missing_in_v3"] == []
        assert result["new_in_v3"] == []

    def test_categories_added_in_v3(self):
        df_v2 = pd.DataFrame({"category": ["A", "B"] * 10})
        df_v3 = pd.DataFrame({"category": ["A", "B", "C", "D"] * 10})
        result = compare_categories(df_v2, df_v3)
        assert result["missing_in_v3"] == []
        assert sorted(result["new_in_v3"]) == ["C", "D"]

    def test_categories_missing_in_v3(self):
        df_v2 = pd.DataFrame({"category": ["A", "B", "C"] * 10})
        df_v3 = pd.DataFrame({"category": ["A", "B"] * 10})
        result = compare_categories(df_v2, df_v3)
        assert result["missing_in_v3"] == ["C"]


class TestComparePhases:
    def test_balanced_phases(self):
        df = pd.DataFrame({"phase": ["early"] * 30 + ["mid"] * 35 + ["late"] * 35})
        result = compare_phases(df, df)
        assert result["v2"]["mid"] >= 0.30
        assert result["v3"]["mid"] >= 0.30
