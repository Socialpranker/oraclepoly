"""Тесты для anti_smart_money_analysis.py (Phase 3.B)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "research"))

from anti_smart_money_analysis import (  # noqa: E402
    MIN_LOSS_USD,
    MIN_TRADES,
    PROMOTION_MIN_STABLE_LOSERS,
    STABILITY_THRESHOLD,
    filter_loser_candidates,
)


class TestFilterLoserCandidates:
    @pytest.fixture
    def sample_scores(self):
        return pd.DataFrame({
            "address": ["w1", "w2", "w3", "w4", "w5"],
            "total_trades": [50, 150, 200, 80, 500],   # w1 below MIN_TRADES
            "total_pnl": [-2000, -500, -1500, -3000, 100],  # w2 not enough loss, w5 profit
            "win_rate": [0.3, 0.45, 0.4, 0.35, 0.55],
        })

    def test_filters_low_trades(self, sample_scores):
        result = filter_loser_candidates(sample_scores)
        # w1 (50 trades) excluded
        assert "w1" not in result["address"].values

    def test_filters_low_loss(self, sample_scores):
        result = filter_loser_candidates(sample_scores)
        # w2 (-500 USD) excluded — needs ≤ -1000
        assert "w2" not in result["address"].values

    def test_filters_profitable_wallets(self, sample_scores):
        result = filter_loser_candidates(sample_scores)
        # w5 (+100 USD profit) excluded
        assert "w5" not in result["address"].values

    def test_includes_qualifying_losers(self, sample_scores):
        result = filter_loser_candidates(sample_scores)
        # w3 (200 trades, -1500 USD) and w4 (80 trades, -3000 USD)?
        # w4 has 80 trades < MIN_TRADES (100) — excluded
        # w3 qualifies: 200 trades, -1500 ≤ -1000
        assert "w3" in result["address"].values
        assert "w4" not in result["address"].values

    def test_custom_thresholds(self, sample_scores):
        # С min_trades=50, w1 проходит по trades; pnl=-2000 < -1000 → loss enough
        result = filter_loser_candidates(sample_scores, min_trades=50, min_loss_usd=1000.0)
        assert "w1" in result["address"].values

    def test_missing_total_pnl_raises(self):
        df = pd.DataFrame({"address": ["w1"], "total_trades": [200]})
        with pytest.raises(ValueError, match="total_pnl"):
            filter_loser_candidates(df)

    def test_missing_total_trades_raises(self):
        df = pd.DataFrame({"address": ["w1"], "total_pnl": [-2000]})
        with pytest.raises(ValueError, match="total_trades"):
            filter_loser_candidates(df)


class TestConstants:
    def test_min_trades_reasonable(self):
        # 100 — статистически минимум для меры win_rate
        assert MIN_TRADES >= 50

    def test_min_loss_reasonable(self):
        # $1000 — отсекает мелкие случайные потери
        assert MIN_LOSS_USD >= 500.0

    def test_stability_threshold(self):
        # 0.5 — корреляция должна быть значимой, но не requires perfect persistence
        assert 0.0 < STABILITY_THRESHOLD < 1.0

    def test_promotion_threshold(self):
        # 1000 — достаточно много fade-кандидатов чтобы стратегия работала
        assert PROMOTION_MIN_STABLE_LOSERS >= 100
