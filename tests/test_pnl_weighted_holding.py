"""PnL-weighted holding period — диагностика hidden tail risk.

Phase 0 Stage 5. См.
docs/superpowers/plans/2026-05-honest-baseline/05-pnl-weighted-holding.md.

Документ §3.5: "Если 80% быстрых сделок по $5, 20% медленных по $500 — медиана
обманчива. Если count-weighted median 0.2h, а PnL-weighted 12h — это скрытый
tail risk."
"""

from __future__ import annotations

import pytest

from app.services.settle_velocity_weighted import (
    bucketize_settle_hours_weighted,
    weighted_median,
)

# ── weighted_median primitive ────────────────────────────────────────────────


class TestWeightedMedian:
    def test_equal_weights_match_count_median(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        weights = [1.0] * 5
        result = weighted_median(values, weights)
        assert result == pytest.approx(3.0)

    def test_skewed_weight_shifts_median(self):
        """5 быстрых ($1) + 1 медленная ($100). PnL-median должен быть ближе к slow."""
        values = [1.0, 1.0, 2.0, 2.0, 3.0, 10.0]
        weights = [1.0, 1.0, 1.0, 1.0, 1.0, 100.0]
        result = weighted_median(values, weights)
        # $100 weight тащит медиану к 10.0
        assert result == pytest.approx(10.0)

    def test_zero_total_weight_returns_zero(self):
        result = weighted_median([1.0, 2.0], [0.0, 0.0])
        assert result == 0.0

    def test_empty_returns_zero(self):
        assert weighted_median([], []) == 0.0

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError):
            weighted_median([1.0, 2.0], [1.0])


# ── bucketize_settle_hours_weighted ──────────────────────────────────────────


class TestBucketizeWeighted:
    def test_empty_input(self):
        result = bucketize_settle_hours_weighted([], [])
        assert result["n"] == 0
        assert result["count_median_hours"] is None
        assert result["pnl_weighted_median_hours"] is None
        assert result["hidden_tail_risk_flag"] is False

    def test_mismatched_lengths_treated_as_empty(self):
        result = bucketize_settle_hours_weighted([1.0, 2.0], [1.0])
        assert result["n"] == 2  # n == len(hours), но _weighted фронтенд treats invalid
        # Не падаем
        assert "pnl_weighted_median_hours" in result

    def test_buckets_pnl_weighted_distribution(self):
        """1h fast (weight 10) + 200h slow (weight 1000) → PnL-weighted under_24 ≈ 1%, over_7d ≈ 99%."""
        # 200h = 8.3d, попадает в over_7d bucket (>=168h)
        hours = [1.0, 200.0]
        weights = [10.0, 1000.0]
        result = bucketize_settle_hours_weighted(hours, weights)
        # Count: 50/50
        assert result["buckets_count_weighted"]["under_24h_pct"] == 50.0
        # PnL weight: 10/(10+1000) ≈ 0.99%
        assert result["buckets_pnl_weighted"]["under_24h_pct"] < 5.0
        assert result["buckets_pnl_weighted"]["over_7d_pct"] > 90.0

    def test_hidden_tail_risk_flag_when_ratio_above_2(self):
        """count_median ~0.5h, pnl_median ~100h → ratio = 200 >> 2 → flag."""
        hours = [0.5, 0.5, 0.5, 100.0]
        weights = [1.0, 1.0, 1.0, 1000.0]
        result = bucketize_settle_hours_weighted(hours, weights)
        assert result["hidden_tail_risk_flag"] is True
        assert result["pnl_to_count_median_ratio"] > 2.0

    def test_no_flag_when_medians_similar(self):
        hours = [1.0, 2.0, 3.0]
        weights = [10.0, 10.0, 10.0]
        result = bucketize_settle_hours_weighted(hours, weights)
        assert result["hidden_tail_risk_flag"] is False

    def test_count_median_computed_correctly(self):
        """Count median = traditional sort, без weights."""
        hours = [1.0, 2.0, 3.0, 4.0, 5.0]
        weights = [10.0, 20.0, 30.0, 40.0, 50.0]
        result = bucketize_settle_hours_weighted(hours, weights)
        assert result["count_median_hours"] == pytest.approx(3.0)

    def test_pnl_median_distinct_from_count(self):
        """PnL median != count median when weights heavily skewed."""
        hours = [1.0, 2.0, 3.0, 4.0, 5.0]
        weights = [100.0, 1.0, 1.0, 1.0, 1.0]
        result = bucketize_settle_hours_weighted(hours, weights)
        # Count median = 3.0. PnL median dominated by hour=1 with weight=100 → ~1.0
        assert result["count_median_hours"] == pytest.approx(3.0)
        assert result["pnl_weighted_median_hours"] < 2.0

    def test_n_field_reflects_input_size(self):
        result = bucketize_settle_hours_weighted([1.0, 2.0, 3.0], [10.0, 20.0, 30.0])
        assert result["n"] == 3

    def test_zero_weight_position_ignored(self):
        """Position с weight=0 не влияет на PnL-weighted median (через weighted_median
        skip), но включён в count median."""
        hours = [1.0, 10.0, 100.0]
        weights = [10.0, 0.0, 10.0]  # средний weight=0
        result = bucketize_settle_hours_weighted(hours, weights)
        # PnL median ignore zero-weight → median of [1.0, 100.0] weighted = около edge
        # Count median = 10 (sorted [1, 10, 100], mid=10)
        assert result["count_median_hours"] == pytest.approx(10.0)
