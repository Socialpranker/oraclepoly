"""Тесты для whale_scoring module."""

import pytest

from app.services.whale_scoring import (
    bayesian_win_rate,
    build_confluence_map,
    calibrated_kelly,
    is_whale_qualified_in_category,
)


class TestBayesianWinRate:
    def test_strong_record(self):
        """30/50 = 60% raw → adj = (30+2)/(50+4) ≈ 59.3%."""
        result = bayesian_win_rate(30, 20)
        assert result == pytest.approx(0.593, abs=0.001)

    def test_small_sample_shrinks_to_prior(self):
        """3/0 → adj = (3+2)/(3+0+4) = 5/7 ≈ 71.4%."""
        result = bayesian_win_rate(3, 0)
        assert result == pytest.approx(0.714, abs=0.001)

    def test_no_data_returns_prior_mean(self):
        """0/0 → prior mean = α/(α+β) = 2/4 = 50%."""
        result = bayesian_win_rate(0, 0)
        assert result == pytest.approx(0.5, abs=0.001)

    def test_large_sample_converges_to_raw(self):
        """100/150 = 66.7% raw → adj ≈ 66.2%."""
        result = bayesian_win_rate(100, 50)
        assert result == pytest.approx(0.662, abs=0.001)

    def test_custom_prior(self):
        """Custom α=5, β=5 → stronger shrinkage."""
        result = bayesian_win_rate(7, 3, prior_alpha=5.0, prior_beta=5.0)
        assert result == pytest.approx(0.6, abs=0.001)

    def test_all_losses(self):
        """0/10 = 0% raw → adj = 2/14 ≈ 14.3%."""
        result = bayesian_win_rate(0, 10)
        assert result == pytest.approx(0.143, abs=0.001)


class TestIsWhaleQualifiedInCategory:
    def test_qualified_whale(self):
        """7 wins, 3 losses (10 trades) → adj_wr = 9/14 ≈ 64.3% > 55%."""
        qualified, adj_wr = is_whale_qualified_in_category(7, 3)
        assert qualified is True
        assert adj_wr == pytest.approx(0.643, abs=0.001)

    def test_not_enough_trades(self):
        """3 wins, 1 loss (4 trades) < min_trades=5."""
        qualified, adj_wr = is_whale_qualified_in_category(3, 1)
        assert qualified is False
        assert adj_wr == 0.0

    def test_low_win_rate_after_shrinkage(self):
        """3 wins, 4 losses (7 trades) → adj_wr = 5/11 ≈ 45.5% < 55%."""
        qualified, adj_wr = is_whale_qualified_in_category(3, 4)
        assert qualified is False
        assert adj_wr == pytest.approx(0.455, abs=0.001)

    def test_exactly_at_threshold(self):
        """Custom min_adjusted_wr=0.60, adj_wr = exactly 0.60."""
        qualified, adj_wr = is_whale_qualified_in_category(4, 2, min_adjusted_wr=0.60)
        assert qualified is True
        assert adj_wr == pytest.approx(0.60, abs=0.001)

    def test_zero_trades(self):
        """0/0 < min_trades."""
        qualified, adj_wr = is_whale_qualified_in_category(0, 0)
        assert qualified is False
        assert adj_wr == 0.0

    def test_custom_min_trades(self):
        """3 wins, 0 losses (3 trades) with min_trades=3 → adj_wr = 5/7 ≈ 71.4%."""
        qualified, adj_wr = is_whale_qualified_in_category(3, 0, min_trades=3)
        assert qualified is True
        assert adj_wr == pytest.approx(0.714, abs=0.001)


class TestBuildConfluenceMap:
    def test_single_whale_single_market(self):
        txs = [{"market_id": "m1", "whale_id": "w1", "tx_outcome": "YES"}]
        result = build_confluence_map(txs)
        assert result == {("m1", "YES"): 1}

    def test_two_whales_same_side(self):
        txs = [
            {"market_id": "m1", "whale_id": "w1", "tx_outcome": "YES"},
            {"market_id": "m1", "whale_id": "w2", "tx_outcome": "YES"},
        ]
        result = build_confluence_map(txs)
        assert result == {("m1", "YES"): 2}

    def test_same_whale_counted_once(self):
        txs = [
            {"market_id": "m1", "whale_id": "w1", "tx_outcome": "YES"},
            {"market_id": "m1", "whale_id": "w1", "tx_outcome": "YES"},
        ]
        result = build_confluence_map(txs)
        assert result == {("m1", "YES"): 1}

    def test_different_sides_separate(self):
        txs = [
            {"market_id": "m1", "whale_id": "w1", "tx_outcome": "YES"},
            {"market_id": "m1", "whale_id": "w2", "tx_outcome": "NO"},
        ]
        result = build_confluence_map(txs)
        assert result[("m1", "YES")] == 1
        assert result[("m1", "NO")] == 1

    def test_multiple_markets(self):
        txs = [
            {"market_id": "m1", "whale_id": "w1", "tx_outcome": "YES"},
            {"market_id": "m1", "whale_id": "w2", "tx_outcome": "YES"},
            {"market_id": "m2", "whale_id": "w1", "tx_outcome": "NO"},
        ]
        result = build_confluence_map(txs)
        assert result[("m1", "YES")] == 2
        assert result[("m2", "NO")] == 1

    def test_empty_list(self):
        result = build_confluence_map([])
        assert result == {}

    def test_lowercase_outcome_normalized_to_upper(self):
        txs = [{"market_id": "m1", "whale_id": "w1", "tx_outcome": "yes"}]
        result = build_confluence_map(txs)
        assert result == {("m1", "YES"): 1}


class TestCalibratedKelly:
    """Единая функция shrinkage для обоих pipelines.

    Oracle: quality_score = rolling Brier Score (lower = better).
    Whale:  quality_score = 1.0 - adjusted_wr (lower = better).
    Pipeline parameter selects threshold table.
    """

    # --- Oracle Sim ---

    def test_oracle_excellent_brier(self):
        """Brier < 0.10 → k=0.50."""
        result = calibrated_kelly(0.20, quality_score=0.08, pipeline="oracle")
        assert result == pytest.approx(0.10, abs=0.001)

    def test_oracle_good_brier(self):
        """Brier 0.12 → 0.10 < q ≤ 0.15 → k=0.33."""
        result = calibrated_kelly(0.20, quality_score=0.12, pipeline="oracle")
        assert result == pytest.approx(0.066, abs=0.001)

    def test_oracle_mediocre_brier(self):
        """Brier 0.18 → 0.15 < q ≤ 0.20 → k=0.25."""
        result = calibrated_kelly(0.20, quality_score=0.18, pipeline="oracle")
        assert result == pytest.approx(0.05, abs=0.001)

    def test_oracle_bad_brier_stops(self):
        """Brier > 0.20 → k=0.10."""
        result = calibrated_kelly(0.20, quality_score=0.25, pipeline="oracle")
        assert result == pytest.approx(0.02, abs=0.001)

    # --- Whale Copy ---

    def test_whale_excellent_wr(self):
        """WR 0.70 → quality = 0.30 < 0.35 → k=0.50."""
        result = calibrated_kelly(0.15, quality_score=0.30, pipeline="whale_copy")
        assert result == pytest.approx(0.075, abs=0.001)

    def test_whale_good_wr(self):
        """WR 0.62 → quality = 0.38 → 0.35 < q ≤ 0.40 → k=0.33."""
        result = calibrated_kelly(0.15, quality_score=0.38, pipeline="whale_copy")
        assert result == pytest.approx(0.05, abs=0.001)

    def test_whale_mediocre_wr(self):
        """WR 0.57 → quality = 0.43 → 0.40 < q ≤ 0.45 → k=0.25."""
        result = calibrated_kelly(0.15, quality_score=0.43, pipeline="whale_copy")
        assert result == pytest.approx(0.0375, abs=0.001)

    def test_whale_poor_wr_stops(self):
        """WR 0.50 → quality = 0.50 → 0.45 < q ≤ 0.50 → k=0.10."""
        result = calibrated_kelly(0.15, quality_score=0.50, pipeline="whale_copy")
        assert result == pytest.approx(0.015, abs=0.001)

    def test_whale_terrible_wr_full_stop(self):
        """WR 0.40 → quality = 0.60 → q > 0.50 → k=0.00."""
        result = calibrated_kelly(0.20, quality_score=0.60, pipeline="whale_copy")
        assert result == 0.0

    # --- Edge cases ---

    def test_no_data_conservative(self):
        """quality_score=None → k=0.15."""
        result = calibrated_kelly(0.20, quality_score=None, pipeline="oracle")
        assert result == pytest.approx(0.03, abs=0.001)

    def test_zero_f_star(self):
        """f_star=0 → always 0."""
        result = calibrated_kelly(0.0, quality_score=0.05, pipeline="oracle")
        assert result == 0.0

    def test_boundary_oracle_0_10(self):
        """Oracle: quality ровно 0.10 → ≤0.10 → k=0.50."""
        result = calibrated_kelly(1.0, quality_score=0.10, pipeline="oracle")
        assert result == pytest.approx(0.50, abs=0.001)

    def test_boundary_whale_0_35(self):
        """Whale: quality ровно 0.35 → ≤0.35 → k=0.50."""
        result = calibrated_kelly(1.0, quality_score=0.35, pipeline="whale_copy")
        assert result == pytest.approx(0.50, abs=0.001)

    def test_unknown_pipeline_raises(self):
        """Неизвестный pipeline → ValueError."""
        with pytest.raises(ValueError, match="Unknown pipeline"):
            calibrated_kelly(0.20, quality_score=0.10, pipeline="invalid")
