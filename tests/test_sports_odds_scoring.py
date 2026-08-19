"""Тесты: sports odds factor в oracle score."""

from __future__ import annotations

from app.core.scoring import BayesianScoreCalculator

# Explicit weights с w_sports_odds=0.5 для проверки формулы.
# DEFAULT_WEIGHTS.w_sports_odds=0.0 (в проде ещё нет live-данных).
_SPORTS_WEIGHTS = {"w_mkt": 1.0, "w_sports_odds": 0.5}


class TestSportsOddsFactor:
    def setup_method(self):
        self.calc = BayesianScoreCalculator()

    def test_sports_odds_shifts_score_down(self):
        """Bookmaker says lower → oracle должен сдвинуться вниз."""
        result = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[],
            sports_odds_factor=-0.5,
            weights=_SPORTS_WEIGHTS,
            temperature=1.0,
        )
        assert result["oracle_score"] < 0.50

    def test_sports_odds_shifts_score_up(self):
        """Bookmaker says higher → oracle должен сдвинуться вверх."""
        result = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[],
            sports_odds_factor=0.5,
            weights=_SPORTS_WEIGHTS,
            temperature=1.0,
        )
        assert result["oracle_score"] > 0.50

    def test_no_sports_odds_no_change(self):
        """Без sports odds — oracle ≈ market_price."""
        result_without = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[],
            weights=_SPORTS_WEIGHTS,
            temperature=1.0,
        )
        result_with_zero = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[],
            sports_odds_factor=0.0,
            weights=_SPORTS_WEIGHTS,
            temperature=1.0,
        )
        assert abs(result_without["oracle_score"] - result_with_zero["oracle_score"]) < 0.001

    def test_sports_odds_raw_in_result(self):
        """Result dict должен содержать sports_odds_raw."""
        result = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[],
            sports_odds_factor=0.3,
        )
        assert "sports_odds_raw" in result
        assert abs(result["sports_odds_raw"] - 0.3) < 0.001

    def test_sports_odds_contribution_in_result(self):
        """Result dict должен содержать sports_odds_contribution с w_sports_odds=0.5."""
        result = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[],
            sports_odds_factor=0.4,
            weights=_SPORTS_WEIGHTS,
        )
        assert "sports_odds_contribution" in result
        # w_sports_odds=0.5 * factor=0.4 = 0.2
        assert abs(result["sports_odds_contribution"] - 0.2) < 0.001

    def test_full_score_passes_sports_odds(self):
        """calculate_full_score прокидывает sports_odds_factor."""
        calc = BayesianScoreCalculator(weights=_SPORTS_WEIGHTS)
        result = calc.calculate_full_score(
            market_price=0.50,
            whale_inputs=[],
            microstructure=None,
            sports_odds_factor=0.5,
            temperature=1.0,
        )
        assert result["sports_odds_raw"] == 0.5
        assert result["oracle_score"] > 0.50
