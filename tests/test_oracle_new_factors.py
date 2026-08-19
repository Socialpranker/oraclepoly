"""
Тесты Phase 2: новые Bayes Factor-ы для Oracle.

maker_direction_bayes_factor — из maker_direction_cache (CLOB /book)
taker_imbalance_bayes_factor — contrarian retail taker flow
horizon_adjustment_multiplier — execution-layer множитель EV по горизонту
"""

from app.core.scoring import BayesianScoreCalculator


def test_maker_direction_bayes_factor_bullish():
    calc = BayesianScoreCalculator()
    lam = calc.maker_direction_bayes_factor(maker_direction_ratio=0.6)
    # 0.6 → bullish → positive
    assert lam > 0
    assert abs(lam) <= 0.8


def test_maker_direction_bayes_factor_bearish():
    calc = BayesianScoreCalculator()
    lam = calc.maker_direction_bayes_factor(maker_direction_ratio=-0.5)
    assert lam < 0
    assert abs(lam) <= 0.8


def test_maker_direction_bayes_factor_neutral():
    calc = BayesianScoreCalculator()
    lam = calc.maker_direction_bayes_factor(maker_direction_ratio=0.0)
    assert abs(lam) < 1e-6


def test_maker_direction_clamped():
    calc = BayesianScoreCalculator()
    lam = calc.maker_direction_bayes_factor(maker_direction_ratio=1.0)
    assert lam <= 0.8


def test_taker_imbalance_bayes_factor_mild_buy():
    calc = BayesianScoreCalculator()
    # buy_mild: buy_ratio 0.55-0.70 → negative (contrarian)
    lam = calc.taker_imbalance_bayes_factor(buy_ratio=0.62)
    assert lam < 0


def test_taker_imbalance_bayes_factor_extreme():
    calc = BayesianScoreCalculator()
    # extreme buy (>0.80) → also negative but different magnitude
    lam_mild = calc.taker_imbalance_bayes_factor(buy_ratio=0.62)
    lam_extreme = calc.taker_imbalance_bayes_factor(buy_ratio=0.85)
    # both negative
    assert lam_mild < 0
    assert lam_extreme < 0


def test_taker_imbalance_symmetric():
    calc = BayesianScoreCalculator()
    lam = calc.taker_imbalance_bayes_factor(buy_ratio=0.5)
    assert abs(lam) < 1e-6


def test_taker_imbalance_clamped():
    calc = BayesianScoreCalculator()
    lam = calc.taker_imbalance_bayes_factor(buy_ratio=1.0)
    assert abs(lam) <= 0.3


def test_horizon_adjustment_short():
    calc = BayesianScoreCalculator()
    mult = calc.horizon_adjustment_multiplier(days_to_resolution=3.0)
    # Short horizon: discount factor
    assert mult < 1.0


def test_horizon_adjustment_sweet_spot():
    calc = BayesianScoreCalculator()
    mult = calc.horizon_adjustment_multiplier(days_to_resolution=20.0)
    # 14-30d sweet spot: EV/$ +0.040
    assert mult > 1.0


def test_horizon_adjustment_long():
    calc = BayesianScoreCalculator()
    mult = calc.horizon_adjustment_multiplier(days_to_resolution=60.0)
    # >30d: slightly discounted (uncertainty)
    assert mult < calc.horizon_adjustment_multiplier(days_to_resolution=20.0)


def test_horizon_adjustment_none():
    calc = BayesianScoreCalculator()
    mult = calc.horizon_adjustment_multiplier(days_to_resolution=None)
    assert mult == 1.0


def test_calculate_oracle_score_includes_maker_direction():
    calc = BayesianScoreCalculator()
    result_without = calc.calculate_oracle_score(
        market_price=0.5,
        whale_factors=[],
        maker_direction_factor=0.0,
    )
    result_with = calc.calculate_oracle_score(
        market_price=0.5,
        whale_factors=[],
        maker_direction_factor=0.5,  # strong bullish
    )
    # maker_direction pushes score up
    assert result_with["oracle_score"] >= result_without["oracle_score"]


def test_calculate_oracle_score_includes_taker_imbalance():
    calc = BayesianScoreCalculator()
    result_neutral = calc.calculate_oracle_score(
        market_price=0.5,
        whale_factors=[],
        taker_imbalance_factor=0.0,
    )
    result_contra = calc.calculate_oracle_score(
        market_price=0.5,
        whale_factors=[],
        taker_imbalance_factor=-0.2,  # contrarian
    )
    assert result_contra["oracle_score"] <= result_neutral["oracle_score"]


def test_new_factors_in_default_weights():
    assert "w_maker_direction" in BayesianScoreCalculator.DEFAULT_WEIGHTS
    assert "w_taker_imbalance" in BayesianScoreCalculator.DEFAULT_WEIGHTS


def test_calculate_full_score_passes_maker_direction_ratio():
    """calculate_full_score должен вычислять maker_direction_factor из ratio."""
    calc = BayesianScoreCalculator()
    result_none = calc.calculate_full_score(
        market_price=0.5,
        whale_inputs=[],
        microstructure=None,
        maker_direction_ratio=None,
    )
    result_bullish = calc.calculate_full_score(
        market_price=0.5,
        whale_inputs=[],
        microstructure=None,
        maker_direction_ratio=0.5,
    )
    assert result_bullish["oracle_score"] >= result_none["oracle_score"]
    assert result_bullish["maker_direction_factor_raw"] != 0.0


def test_calculate_full_score_passes_taker_buy_ratio():
    """calculate_full_score должен вычислять taker_imbalance_factor из buy_ratio."""
    calc = BayesianScoreCalculator()
    result_none = calc.calculate_full_score(
        market_price=0.5,
        whale_inputs=[],
        microstructure=None,
        taker_buy_ratio=None,
    )
    result_high_buy = calc.calculate_full_score(
        market_price=0.5,
        whale_inputs=[],
        microstructure=None,
        taker_buy_ratio=0.62,  # mild buy → contrarian (fade)
    )
    # Contrarian: retail buys → factor <0 → weight +0.15 → contribution <0 → score down
    assert result_high_buy["oracle_score"] <= result_none["oracle_score"]
    assert result_high_buy["taker_imbalance_factor_raw"] < 0
