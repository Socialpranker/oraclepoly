"""
Тесты для Bayesian Log-Odds Oracle Score Calculator.

Покрывает все Sub-steps 3.1-3.6 из MATH_SPEC.
"""

import math

import pytest

from app.core.scoring import (
    BayesianScoreCalculator,
    Calibrator,
    safe_logit,
    stable_sigmoid,
    whale_profile_multiplier,
)

# =====================================================================
# Sub-step 3.1: Core Coordinate Transformations
# =====================================================================


class TestSafeLogit:
    def test_midpoint(self):
        """p=0.5 -> Z=0 (нейтральный prior)."""
        assert safe_logit(0.5) == 0.0

    def test_high(self):
        """p=0.9 -> Z ~ 2.197 (положительный)."""
        assert abs(safe_logit(0.9) - math.log(0.9 / 0.1)) < 1e-10

    def test_low(self):
        """p=0.1 -> Z ~ -2.197 (отрицательный)."""
        assert abs(safe_logit(0.1) - math.log(0.1 / 0.9)) < 1e-10

    def test_zero(self):
        """p=0.0 -> clamped к eps -> большое отрицательное число (НЕ -inf)."""
        z = safe_logit(0.0)
        assert math.isfinite(z)
        assert z < -10  # ln(1e-5 / (1 - 1e-5)) ~ -11.51

    def test_one(self):
        """p=1.0 -> clamped к 1-eps -> большое положительное число (НЕ +inf)."""
        z = safe_logit(1.0)
        assert math.isfinite(z)
        assert z > 10


class TestStableSigmoid:
    def test_zero(self):
        """Z=0 -> p=0.5."""
        assert stable_sigmoid(0.0) == 0.5

    def test_positive(self):
        """Z=2.0 -> p ~ 0.8808."""
        assert abs(stable_sigmoid(2.0) - 1 / (1 + math.exp(-2))) < 1e-10

    def test_negative(self):
        """Z=-2.0 -> p ~ 0.1192."""
        assert abs(stable_sigmoid(-2.0) - math.exp(-2) / (1 + math.exp(-2))) < 1e-10

    def test_extreme_positive(self):
        """Z=1000 -> p ~ 1.0 (не overflow, не NaN)."""
        p = stable_sigmoid(1000.0)
        assert math.isfinite(p)
        assert p > 0.999

    def test_extreme_negative(self):
        """Z=-1000 -> p ~ 0.0 (не overflow, не NaN)."""
        p = stable_sigmoid(-1000.0)
        assert math.isfinite(p)
        assert p < 0.001


class TestRoundtrip:
    def test_roundtrip(self):
        """logit -> sigmoid -> ~ original p."""
        for p in [0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99]:
            recovered = stable_sigmoid(safe_logit(p))
            assert abs(recovered - p) < 1e-4, f"Failed for p={p}: got {recovered}"

    def test_symmetry(self):
        """logit(p) = -logit(1-p) — антисимметрия."""
        for p in [0.2, 0.3, 0.7, 0.8]:
            assert abs(safe_logit(p) + safe_logit(1 - p)) < 1e-10


# =====================================================================
# Sub-step 3.2: Kelly Inversion + Laplace Smoothing
# =====================================================================


class TestKellyFraction:
    def setup_method(self):
        self.calc = BayesianScoreCalculator()

    def test_normal(self):
        """Нормальный случай: $30K stake, $200K bankroll -> f=0.15."""
        assert abs(self.calc.kelly_fraction(30_000, 200_000) - 0.15) < 1e-6

    def test_capped(self):
        """Большая ставка: f_raw > 0.25 -> capped at 0.25."""
        assert self.calc.kelly_fraction(100_000, 200_000) == 0.25

    def test_zero_bankroll(self):
        """bankroll=0 -> guard -> f capped at f_max."""
        f = self.calc.kelly_fraction(10_000, 0.0)
        assert f == 0.25

    def test_tiny_stake(self):
        """Маленькая ставка от большого кита -> маленький f."""
        f = self.calc.kelly_fraction(1_000, 1_000_000)
        assert abs(f - 0.001) < 1e-6


class TestLaplaceCredibility:
    def setup_method(self):
        self.calc = BayesianScoreCalculator()

    def test_new_whale(self):
        """Новый кит (0, 0) -> C = 0.5."""
        assert self.calc.laplace_credibility(0, 0) == 0.5

    def test_experienced(self):
        """100 wins / 20 losses -> C ~ 0.828."""
        c = self.calc.laplace_credibility(100, 20)
        assert abs(c - 101 / 122) < 1e-6

    def test_all_wins(self):
        """10 wins / 0 losses -> C ~ 0.917 (не 1.0!)."""
        c = self.calc.laplace_credibility(10, 0)
        assert abs(c - 11 / 12) < 1e-6
        assert c < 1.0

    def test_all_losses(self):
        """0 wins / 10 losses -> C ~ 0.083 (не 0.0!)."""
        c = self.calc.laplace_credibility(0, 10)
        assert abs(c - 1 / 12) < 1e-6
        assert c > 0.0


class TestWhaleImpliedProbability:
    def setup_method(self):
        self.calc = BayesianScoreCalculator()

    def test_normal(self):
        """f=0.15, b=0.667 -> p_raw ~ 0.660."""
        p = self.calc.whale_implied_probability(0.15, 0.667)
        expected = (0.15 * 0.667 + 1) / (0.667 + 1)
        assert abs(p - expected) < 1e-3

    def test_zero_f(self):
        """f=0 -> p_raw = 1/(b+1) = market_price (кит не ставит -> no conviction)."""
        p = self.calc.whale_implied_probability(0.0, 0.667)
        assert abs(p - 1 / 1.667) < 1e-3


class TestWhaleBayesFactor:
    def setup_method(self):
        self.calc = BayesianScoreCalculator()

    def test_bullish(self):
        """Крупная ставка BUY от опытного кита -> положительный Lambda."""
        whale_input = {
            "S": 50_000,
            "B_est": 200_000,
            "b": 0.667,
            "W": 80,
            "L": 20,
            "direction": "buy",
            "current_price": 0.60,
            "outcome": "YES",
        }
        lam = self.calc.whale_bayes_factor(whale_input)
        assert lam > 0

    def test_sell(self):
        """SELL от кита -> отрицательный Lambda (медвежий сигнал)."""
        whale_input = {
            "S": 50_000,
            "B_est": 200_000,
            "b": 0.667,
            "W": 80,
            "L": 20,
            "direction": "sell",
            "current_price": 0.60,
            "outcome": "YES",
        }
        lam = self.calc.whale_bayes_factor(whale_input)
        assert lam < 0

    def test_new_whale_halved(self):
        """Новый кит (W=0, L=0): confidence_scale=0.1 + low credibility → сильно слабее опытного.

        После fix confidence_scale (track_record = max(bet_count, W+L)):
        - Новый кит: track_record=0 → confidence_scale=0.1
        - Опытный кит: track_record=100 → confidence_scale=1.0
        Ratio ~0.05-0.15 (ранее ~0.5 из-за бага, где оба получали 0.1).
        """
        base = {
            "S": 50_000,
            "B_est": 200_000,
            "b": 0.667,
            "direction": "buy",
            "current_price": 0.60,
            "outcome": "YES",
        }

        lam_new = self.calc.whale_bayes_factor({**base, "W": 0, "L": 0})
        lam_exp = self.calc.whale_bayes_factor({**base, "W": 100, "L": 0})

        assert lam_new < lam_exp
        ratio = lam_new / lam_exp if lam_exp != 0 else 0
        assert 0.05 < ratio < 0.15


# Sub-step 3.3: Liquidity Gating — покрытие в tests/test_math_consistency.py::TestLiquidityGate

# =====================================================================
# Sub-step 3.4: Temporal News Decay
# =====================================================================


class TestTemporalDecay:
    def setup_method(self):
        self.calc = BayesianScoreCalculator(decay_alpha=0.0)  # explicit exponential for legacy tests

    def test_zero_hours(self):
        """dt=0 -> decay=1.0 -> полный сигнал."""
        assert self.calc.temporal_decay(1.0, 0.0) == 1.0

    def test_half_life(self):
        """dt=H -> сигнал = 50%."""
        result = self.calc.temporal_decay(1.0, 24.0, half_life_hours=24.0)
        assert abs(result - 0.5) < 1e-6

    def test_double_half_life(self):
        """dt=2H -> сигнал = 25%."""
        result = self.calc.temporal_decay(1.0, 48.0, half_life_hours=24.0)
        assert abs(result - 0.25) < 1e-6

    def test_triple_half_life(self):
        """dt=3H -> сигнал = 12.5%."""
        result = self.calc.temporal_decay(1.0, 72.0, half_life_hours=24.0)
        assert abs(result - 0.125) < 1e-6

    def test_preserves_sign(self):
        """Отрицательный Lambda_initial -> отрицательный Lambda_decayed."""
        result = self.calc.temporal_decay(-1.5, 12.0)
        assert result < 0

    def test_scales_magnitude(self):
        """Lambda_initial=2.0, dt=24h -> result=1.0."""
        result = self.calc.temporal_decay(2.0, 24.0, half_life_hours=24.0)
        assert abs(result - 1.0) < 1e-6

    def test_very_old(self):
        """dt=168h (7 дней), H=24h -> сигнал ~ 0.8%."""
        result = self.calc.temporal_decay(1.0, 168.0, half_life_hours=24.0)
        assert abs(result) < 0.01


# =====================================================================
# Sub-step 3.5: Bayesian Aggregation + Black Swan Gating
# =====================================================================


class TestOracleScore:
    def setup_method(self):
        self.calc = BayesianScoreCalculator()

    def test_no_signals(self):
        """Нет whale сигналов, w_mkt=1.0 -> score ~ market_price."""
        result = self.calc.calculate_oracle_score(
            market_price=0.60,
            whale_factors=[],
            weights={"w_mkt": 1.0},
            temperature=1.0,
        )
        # С w_mkt=1.0, T=1.0 sigmoid(logit(0.60)) = 0.60
        assert abs(result["oracle_score"] - 0.60) < 0.03

    def test_bullish_whales(self):
        """Два сильных бычьих кита -> score > market_price (explicit w_whale=0.5)."""
        result = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[0.5, 0.3],
            weights={"w_mkt": 1.0, "w_whale": 0.5, "w_whale_contra": 0.0},
        )
        assert result["oracle_score"] > 0.50

    def test_bearish_whales(self):
        """Два медвежьих кита -> score < market_price (explicit w_whale=0.5)."""
        result = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[-0.5, -0.3],
            weights={"w_mkt": 1.0, "w_whale": 0.5, "w_whale_contra": 0.0},
        )
        assert result["oracle_score"] < 0.50

    def test_mixed_signals(self):
        """Бычий кит + медвежий кит -> частичная компенсация."""
        result = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[0.5, -0.3],
        )
        assert 0.45 < result["oracle_score"] < 0.65

    def test_extreme_conviction(self):
        """Много сильных сигналов -> clamped divergence, не NaN/inf."""
        result = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[2.0] * 10,
            weights={"w_mkt": 1.0, "w_whale": 0.5, "w_whale_contra": 0.0},
        )
        assert 0 < result["oracle_score"] <= 1.0
        assert math.isfinite(result["oracle_score"])
        # Divergence clamp: upward capped at market + MAX_DIVERGENCE (0.10)
        assert result["oracle_score"] <= 0.60 + 0.01  # market 0.5 + max clamp 0.10

    def test_output_format(self):
        """Проверка формата output dict."""
        result = self.calc.calculate_oracle_score(0.50, [0.1])
        assert "oracle_score" in result
        assert "z_posterior" in result
        assert "p_raw" in result
        assert "p_swan" in result
        assert "z_mkt" in result
        assert "whale_contribution" in result
        assert "n_whales" in result

    def test_bounded(self):
        """oracle_score всегда в [0, 1]."""
        for mp in [0.01, 0.1, 0.5, 0.9, 0.99]:
            for wf in [[-5.0], [0.0], [5.0]]:
                result = self.calc.calculate_oracle_score(mp, wf)
                assert 0 <= result["oracle_score"] <= 1.0, (
                    f"Out of bounds: mp={mp}, wf={wf} -> {result['oracle_score']}"
                )


# =====================================================================
# Sub-step 3.5: calculate_full_score (end-to-end)
# =====================================================================


class TestCalculateFullScore:
    def setup_method(self):
        self.calc = BayesianScoreCalculator()

    def test_end_to_end_basic(self):
        """Базовый end-to-end тест с одним китом."""
        whale_inputs = [
            {
                "S": 50_000,
                "B_est": 200_000,
                "b": 0.667,
                "W": 80,
                "L": 20,
                "direction": "buy",
                "current_price": 0.60,
                "outcome": "YES",
                "whale_address": "0xabc",
            }
        ]
        microstructure = {
            "spread": 0.01,
            "depth_yes": 500_000,
            "depth_no": 300_000,
        }

        result = self.calc.calculate_full_score(0.60, whale_inputs, microstructure)
        assert "oracle_score" in result
        assert "whale_details" in result
        assert len(result["whale_details"]) == 1
        assert 0 <= result["oracle_score"] <= 1.0

    def test_end_to_end_no_microstructure(self):
        """Без данных microstructure -> whale factors at 50%."""
        whale_inputs = [
            {
                "S": 30_000,
                "B_est": 200_000,
                "b": 0.667,
                "W": 50,
                "L": 10,
                "direction": "buy",
                "current_price": 0.60,
                "outcome": "YES",
            }
        ]

        result = self.calc.calculate_full_score(0.60, whale_inputs, None)
        assert 0 <= result["oracle_score"] <= 1.0

    def test_end_to_end_empty(self):
        """Нет сигналов, w_mkt=1.0 -> score ~ market_price."""
        calc = BayesianScoreCalculator(weights={"w_mkt": 1.0})
        result = calc.calculate_full_score(0.55, [], None, temperature=1.0)
        assert abs(result["oracle_score"] - 0.55) < 0.02


# =====================================================================
# Sub-step 3.6: Calibrator
# =====================================================================


class TestLogLoss:
    def setup_method(self):
        self.cal = Calibrator()

    def test_perfect(self):
        """Идеальные предсказания -> loss ~ 0."""
        loss = self.cal.log_loss([0.99, 0.01, 0.99], [1, 0, 1])
        assert loss < 0.05

    def test_worst(self):
        """Максимально ошибочные предсказания -> loss > 4."""
        loss = self.cal.log_loss([0.01, 0.99, 0.01], [1, 0, 1])
        assert loss > 4.0

    def test_uncertain(self):
        """Все предсказания = 0.5 -> loss = ln(2) ~ 0.693."""
        loss = self.cal.log_loss([0.5, 0.5, 0.5], [1, 0, 1])
        assert abs(loss - math.log(2)) < 0.01

    def test_l2_increases(self):
        """L2 term увеличивает loss."""
        loss_no_l2 = self.cal.log_loss([0.7, 0.3], [1, 0])
        loss_with_l2 = self.cal.log_loss([0.7, 0.3], [1, 0], alpha=0.1, weights=[1.0, 0.8, 0.5])
        assert loss_with_l2 > loss_no_l2


class TestBrierScore:
    def setup_method(self):
        self.cal = Calibrator()

    def test_perfect(self):
        """Идеальные предсказания -> BS = 0."""
        bs = self.cal.brier_score([1.0, 0.0, 1.0], [1, 0, 1])
        assert bs == 0.0

    def test_random(self):
        """Все предсказания = 0.5 -> BS = 0.25."""
        bs = self.cal.brier_score([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0])
        assert abs(bs - 0.25) < 1e-6

    def test_bad(self):
        """Максимально ошибочные -> BS = 1.0."""
        bs = self.cal.brier_score([0.0, 1.0], [1, 0])
        assert abs(bs - 1.0) < 1e-6


class TestTemperatureScale:
    def setup_method(self):
        self.cal = Calibrator()

    def test_identity(self):
        """T=1.0 -> без изменений."""
        z = 1.0
        p_original = stable_sigmoid(z)
        p_scaled = self.cal.temperature_scale(z, temperature=1.0)
        assert abs(p_scaled - p_original) < 1e-10

    def test_softens(self):
        """T=2.0 -> уверенность уменьшается (ближе к 0.5)."""
        z = 2.0
        p_scaled = self.cal.temperature_scale(z, temperature=2.0)
        p_original = stable_sigmoid(z)
        assert abs(p_scaled - 0.5) < abs(p_original - 0.5)

    def test_zero_z(self):
        """Z=0 -> p=0.5 при любом T."""
        for t in [0.5, 1.0, 2.0, 5.0]:
            assert abs(self.cal.temperature_scale(0.0, t) - 0.5) < 1e-10


class TestFindOptimalTemperature:
    def test_overconfident(self):
        """Grid search находит T > 1 для overconfident predictions."""
        cal = Calibrator()
        z_posteriors = [2.0, -2.0, 1.5, -1.5, 2.5, -2.5]
        outcomes = [1, 0, 0, 1, 1, 0]
        t_opt = cal.find_optimal_temperature(z_posteriors, outcomes)
        assert t_opt > 1.0


# =====================================================================
# compute_analytics
# =====================================================================


class TestComputeAnalytics:
    def setup_method(self):
        self.calc = BayesianScoreCalculator()

    def test_underpriced(self):
        """oracle > price + 0.10 -> underpriced."""
        result = self.calc.compute_analytics(
            oracle_score=0.75,
            current_price=0.50,
            previous_oracle_score=None,
            z_posterior=1.0,
            n_whales=5,
        )
        assert result["divergence"]["signal"] == "underpriced"
        assert result["divergence"]["magnitude"] == "strong"

    def test_overpriced(self):
        """oracle < price - 0.10 -> overpriced."""
        result = self.calc.compute_analytics(
            oracle_score=0.30,
            current_price=0.60,
            previous_oracle_score=None,
            z_posterior=-0.5,
            n_whales=3,
        )
        assert result["divergence"]["signal"] == "overpriced"

    def test_fair(self):
        """oracle ~ price -> fair."""
        result = self.calc.compute_analytics(
            oracle_score=0.55,
            current_price=0.50,
            previous_oracle_score=None,
            z_posterior=0.2,
            n_whales=2,
        )
        assert result["divergence"]["signal"] == "fair"

    def test_trend_rising(self):
        """Текущий score > предыдущий -> rising."""
        result = self.calc.compute_analytics(
            oracle_score=0.60,
            current_price=0.50,
            previous_oracle_score=0.55,
            z_posterior=0.4,
            n_whales=3,
        )
        assert result["trend"]["direction"] == "rising"

    def test_trend_unknown(self):
        """Нет предыдущего score -> unknown."""
        result = self.calc.compute_analytics(
            oracle_score=0.60,
            current_price=0.50,
            previous_oracle_score=None,
            z_posterior=0.4,
            n_whales=3,
        )
        assert result["trend"]["direction"] == "unknown"

    def test_risk_always_none(self):
        """risk_level всегда 'none' (p_swan удалён из pipeline)."""
        result = self.calc.compute_analytics(
            oracle_score=0.10,
            current_price=0.50,
            previous_oracle_score=None,
            z_posterior=-2.0,
            n_whales=1,
        )
        assert result["risk"]["risk_level"] == "none"

    def test_data_quality_excellent(self):
        """Много сигналов -> excellent."""
        result = self.calc.compute_analytics(
            oracle_score=0.60,
            current_price=0.50,
            previous_oracle_score=None,
            z_posterior=0.4,
            n_whales=5,
        )
        assert result["confidence"]["data_quality"] == "excellent"

    def test_data_quality_poor(self):
        """Мало сигналов -> poor."""
        result = self.calc.compute_analytics(
            oracle_score=0.55,
            current_price=0.50,
            previous_oracle_score=None,
            z_posterior=0.2,
            n_whales=0,
        )
        assert result["confidence"]["data_quality"] == "poor"


# =====================================================================
# Smart Money Bayes Factors
# =====================================================================


class TestSmartMoneyBayesFactors:
    def setup_method(self):
        self.calc = BayesianScoreCalculator()

    def test_smart_money_signals_increase_score(self):
        """Положительные smart money сигналы увеличивают oracle score."""
        result_without = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[],
        )
        result_with = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[],
            smart_money_factors={"fresh": [0.5], "cluster": [0.3], "registry": [0.2]},
        )
        assert result_with["oracle_score"] > result_without["oracle_score"]

    def test_no_smart_money_same_as_before(self):
        """Без smart money сигналов результат идентичен базовому."""
        result_none = self.calc.calculate_oracle_score(
            market_price=0.60,
            whale_factors=[0.3],
        )
        result_empty = self.calc.calculate_oracle_score(
            market_price=0.60,
            whale_factors=[0.3],
            smart_money_factors={"fresh": [], "cluster": [], "registry": []},
        )
        assert abs(result_none["oracle_score"] - result_empty["oracle_score"]) < 1e-10

    def test_smart_money_weights_in_defaults(self):
        """DEFAULT_WEIGHTS содержит w_fresh, w_cluster, w_registry, w_whale_contra."""
        assert "w_fresh" in self.calc.DEFAULT_WEIGHTS
        assert "w_cluster" in self.calc.DEFAULT_WEIGHTS
        assert "w_registry" in self.calc.DEFAULT_WEIGHTS
        assert "w_whale_contra" in self.calc.DEFAULT_WEIGHTS
        assert self.calc.DEFAULT_WEIGHTS["w_fresh"] == 0.0  # обнулён (research: шум)
        assert self.calc.DEFAULT_WEIGHTS["w_cluster"] == 0.0  # обнулён (нет данных)
        assert self.calc.DEFAULT_WEIGHTS["w_registry"] == 0.1  # прод global
        assert self.calc.DEFAULT_WEIGHTS["w_whale_contra"] == 0.0  # прод: отключён

    def test_decomposition_includes_smart_money_shifts(self):
        """compute_decomposition включает fresh_wallet_shift, cluster_shift, registry_shift."""
        result = self.calc.calculate_oracle_score(
            market_price=0.50,
            whale_factors=[0.2],
            smart_money_factors={"fresh": [0.4], "cluster": [0.3], "registry": [0.1]},
            weights={
                "w_mkt": 1.0,
                "w_whale": 0.5,
                "w_whale_contra": 0.0,
                "w_fresh": 0.6,
                "w_cluster": 0.1,
                "w_registry": 0.3,
            },
        )
        decomp = BayesianScoreCalculator.compute_decomposition(0.50, result)
        assert "fresh_wallet_shift" in decomp
        assert "cluster_shift" in decomp
        assert "registry_shift" in decomp
        assert decomp["fresh_wallet_shift"] != 0.0


# =====================================================================
# Brier Decomposition (Murphy)
# =====================================================================


class TestBrierDecomposition:
    """Тесты Brier Decomposition (Murphy)."""

    def setup_method(self):
        self.cal = Calibrator()

    def test_perfect_calibration_zero_reliability(self):
        """Идеально откалиброванные предсказания → reliability ≈ 0."""
        predictions = [0.7] * 10
        outcomes = [1, 1, 1, 1, 1, 1, 1, 0, 0, 0]
        result = self.cal.brier_decomposition(predictions, outcomes, n_bins=5)
        assert result["reliability"] < 0.05
        assert "brier_score" in result
        assert "resolution" in result
        assert "uncertainty" in result

    def test_constant_predictions_zero_resolution(self):
        """Все предсказания одинаковые → resolution = 0."""
        predictions = [0.5] * 20
        outcomes = [1] * 10 + [0] * 10
        result = self.cal.brier_decomposition(predictions, outcomes, n_bins=5)
        assert result["resolution"] < 0.01

    def test_uncertainty_equals_base_rate_variance(self):
        """Uncertainty = base_rate × (1 - base_rate)."""
        predictions = [0.6] * 10
        outcomes = [1, 1, 1, 0, 0, 0, 0, 0, 0, 0]  # base_rate = 0.3
        result = self.cal.brier_decomposition(predictions, outcomes, n_bins=5)
        expected_unc = 0.3 * 0.7
        assert abs(result["uncertainty"] - expected_unc) < 0.01

    def test_decomposition_sums_correctly(self):
        """BS = Reliability - Resolution + Uncertainty."""
        predictions = [0.9, 0.8, 0.7, 0.3, 0.2, 0.1]
        outcomes = [1, 1, 1, 0, 0, 0]
        result = self.cal.brier_decomposition(predictions, outcomes, n_bins=3)
        expected_bs = result["reliability"] - result["resolution"] + result["uncertainty"]
        assert abs(result["brier_score"] - expected_bs) < 0.001

    def test_diagnosis_noise_detection(self):
        """Если reliability > resolution → диагностика сообщает о шуме."""
        predictions = [0.9] * 10
        outcomes = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
        result = self.cal.brier_decomposition(predictions, outcomes, n_bins=5)
        assert "diagnosis" in result

    def test_empty_predictions(self):
        """Пустой вход → нулевой результат."""
        result = self.cal.brier_decomposition([], [], n_bins=5)
        assert result["brier_score"] == 0.0


# =====================================================================
# Informed Beta Prior (замена Laplace)
# =====================================================================


class TestInformedCredibility:
    """Тесты Informed Beta Prior (замена Laplace)."""

    def setup_method(self):
        self.calc = BayesianScoreCalculator(
            prior_alpha=5.5,
            prior_beta=4.5,
        )

    def test_newcomer_starts_at_mean(self):
        """Новичок (0W/0L) → C = 5.5/10 = 0.55, не 0.50."""
        c = self.calc.informed_credibility(0, 0)
        assert abs(c - 0.55) < 0.001

    def test_experienced_whale_converges_to_data(self):
        """50W/10L → данные перевешивают приор."""
        c = self.calc.informed_credibility(50, 10)
        assert abs(c - 0.793) < 0.01

    def test_single_win_less_extreme_than_laplace(self):
        """1W/0L: informed=0.591 < laplace=0.667."""
        c_informed = self.calc.informed_credibility(1, 0)
        c_laplace = self.calc.laplace_credibility(1, 0)
        assert c_informed < c_laplace

    def test_bad_whale_higher_than_laplace(self):
        """0W/5L: informed > laplace — приор смягчает."""
        c_informed = self.calc.informed_credibility(0, 5)
        c_laplace = self.calc.laplace_credibility(0, 5)
        assert c_informed > c_laplace

    def test_backward_compat_default(self):
        """По умолчанию (без явных alpha/beta) поведение как Laplace."""
        calc_default = BayesianScoreCalculator()
        c = calc_default.laplace_credibility(5, 5)
        assert abs(c - 0.50) < 0.01


class TestTemporalDiscounting:
    """Тесты дисконтирования W/L по давности."""

    def test_no_discount_for_fresh_trades(self):
        """Малое количество сделок → без дисконтирования."""
        w_eff, l_eff = BayesianScoreCalculator.discounted_record(5, 3, gamma=0.97)
        assert w_eff == 5
        assert l_eff == 3

    def test_discount_reduces_old_record(self):
        """100 сделок с γ=0.97 → эффективно ~50 сделок."""
        w_eff, l_eff = BayesianScoreCalculator.discounted_record(70, 30, gamma=0.97)
        assert w_eff < 70
        assert l_eff < 30
        assert w_eff + l_eff < 100

    def test_gamma_one_no_discount(self):
        """γ=1.0 → без дисконтирования (identity)."""
        w_eff, l_eff = BayesianScoreCalculator.discounted_record(50, 10, gamma=1.0)
        assert w_eff == 50
        assert l_eff == 10


# =====================================================================
# Cluster Discounts в calculate_full_score (v2)
# =====================================================================


class TestClusterDiscountsInScoring:
    """Тесты cluster_discounts в calculate_full_score."""

    def setup_method(self):
        self.calculator = BayesianScoreCalculator()

    def test_no_discounts_default(self):
        """cluster_discounts=None → whale_factors не изменяются."""
        result = self.calculator.calculate_full_score(
            market_price=0.5,
            whale_inputs=[
                {
                    "S": 10000,
                    "B_est": 200_000,
                    "b": 0.667,
                    "W": 5,
                    "L": 3,
                    "direction": "buy",
                    "current_price": 0.50,
                    "whale_address": "0xaaa",
                    "outcome": "YES",
                }
            ],
            microstructure=None,
            cluster_discounts=None,
        )
        assert result["oracle_score"] > 0

    def test_discounted_whale_reduces_score(self):
        """Whale с discount=0.5 → меньший вклад в oracle_score."""
        kwargs = dict(
            market_price=0.5,
            whale_inputs=[
                {
                    "S": 50000,
                    "B_est": 200_000,
                    "b": 0.667,
                    "W": 10,
                    "L": 2,
                    "direction": "buy",
                    "current_price": 0.50,
                    "whale_address": "0xaaa",
                    "outcome": "YES",
                }
            ],
            microstructure=None,
        )
        result_full = self.calculator.calculate_full_score(**kwargs, cluster_discounts=None)
        result_half = self.calculator.calculate_full_score(**kwargs, cluster_discounts={"0xaaa": 0.5})
        # Дисконтированный score ближе к market_price (0.5)
        assert abs(result_half["oracle_score"] - 0.5) < abs(result_full["oracle_score"] - 0.5)


# =====================================================================
# Power Law Decay (замена экспоненты)
# =====================================================================


class TestPowerLawDecay:
    """Тесты степенного распада новостей."""

    def setup_method(self):
        self.calc = BayesianScoreCalculator(decay_alpha=0.52)

    def test_zero_hours_full_signal(self):
        """t=0 → полный сигнал."""
        result = self.calc.temporal_decay(1.0, 0.0)
        assert abs(result - 1.0) < 0.001

    def test_half_life_50_percent(self):
        """t=24h → сигнал ≈ 50% (совпадает по дизайну)."""
        result = self.calc.temporal_decay(1.0, 24.0)
        assert abs(result - 0.5) < 0.05

    def test_long_tail_stronger_than_exp(self):
        """t=72h: power law > exponential (длинный хвост)."""
        calc_exp = BayesianScoreCalculator(decay_alpha=0.0)  # 0.0 = exponential fallback
        power = self.calc.temporal_decay(1.0, 72.0)
        exp = calc_exp.temporal_decay(1.0, 72.0)
        assert power > exp

    def test_very_old_news_near_zero(self):
        """t=500h → почти 0, но не точный 0."""
        result = self.calc.temporal_decay(1.0, 500.0)
        assert result > 0.0
        assert result < 0.15

    def test_negative_time_clamped(self):
        """Отрицательное время → t=0."""
        result = self.calc.temporal_decay(1.0, -5.0)
        assert abs(result - 1.0) < 0.001


# =====================================================================
# ROI-Informed Credibility
# =====================================================================


class TestRoiInformedCredibility:
    """Тесты для ROI-Informed Credibility (замена W/L-based)."""

    def setup_method(self):
        self.calc = BayesianScoreCalculator(prior_alpha=5.5, prior_beta=4.5)

    def test_few_trades_falls_back_to_wl(self):
        """W+L < 10 → используем только W/L credibility."""
        c = self.calc.roi_informed_credibility(wins=5, losses=2, roi_percent=100.0)
        c_wl = self.calc.informed_credibility(5, 2)
        assert c == pytest.approx(c_wl, abs=1e-6)

    def test_many_trades_uses_roi(self):
        """W+L >= 30 → 100% ROI-based."""
        c = self.calc.roi_informed_credibility(wins=20, losses=10, roi_percent=0.0)
        assert c == pytest.approx(0.5, abs=0.01)

    def test_certainty_chaser_deflated(self):
        """Кит 66W/9L (88% WR), ROI = -0.16% → C ≈ 0.5 (не 0.84)."""
        c_old = self.calc.informed_credibility(66, 9)
        c_new = self.calc.roi_informed_credibility(wins=66, losses=9, roi_percent=-0.16)
        assert c_old > 0.83
        assert c_new < 0.55

    def test_profitable_trader_preserved(self):
        """Кит с ROI +48%, 77.8% WR, W+L=9 → fallback на W/L."""
        c = self.calc.roi_informed_credibility(wins=7, losses=2, roi_percent=48.0)
        c_wl = self.calc.informed_credibility(7, 2)
        assert c == pytest.approx(c_wl, abs=1e-6)

    def test_profitable_trader_many_trades(self):
        """Кит 50W/20L, ROI +50% → C > 0.7."""
        c = self.calc.roi_informed_credibility(wins=50, losses=20, roi_percent=50.0)
        assert c > 0.7

    def test_losing_trader_penalized(self):
        """Кит с ROI -4.07%, 161W/4L → C < 0.5."""
        c = self.calc.roi_informed_credibility(wins=161, losses=4, roi_percent=-4.07)
        assert c < 0.5

    def test_transition_zone(self):
        """W+L = 20 → 50% ROI + 50% W/L."""
        c = self.calc.roi_informed_credibility(wins=15, losses=5, roi_percent=0.0)
        c_wl = self.calc.informed_credibility(15, 5)
        c_roi = 0.5
        expected = 0.5 * c_roi + 0.5 * c_wl
        assert c == pytest.approx(expected, abs=0.01)

    def test_extreme_positive_roi(self):
        """ROI = +200% → C_roi ≈ 0.98."""
        c = self.calc.roi_informed_credibility(wins=20, losses=10, roi_percent=200.0)
        assert c > 0.95

    def test_extreme_negative_roi(self):
        """ROI = -200% → C_roi ≈ 0.02."""
        c = self.calc.roi_informed_credibility(wins=20, losses=10, roi_percent=-200.0)
        assert c < 0.05


class TestWhaleBayesFactorRoiCredibility:
    """Проверяем что whale_bayes_factor использует ROI-informed credibility."""

    def setup_method(self):
        self.calc = BayesianScoreCalculator(
            prior_alpha=5.5,
            prior_beta=4.5,
            expert_weight_enabled=False,
        )

    def _make_input(self, wins, losses, roi_percent, price=0.5, stake=10000, bankroll=100000):
        return {
            "S": stake,
            "B_est": bankroll,
            "b": 1.0,
            "W": wins,
            "L": losses,
            "direction": "buy",
            "current_price": price,
            "bet_count": 0,
            "roi_percent": roi_percent,
        }

    def test_certainty_chaser_weaker_signal(self):
        """Кит с 88% WR но ROI ~0% даёт слабее сигнал чем profitable кит."""
        inp_chaser = self._make_input(wins=66, losses=9, roi_percent=-0.16)
        inp_sharp = self._make_input(wins=50, losses=20, roi_percent=50.0)
        lam_chaser = self.calc.whale_bayes_factor(inp_chaser)
        lam_sharp = self.calc.whale_bayes_factor(inp_sharp)
        assert abs(lam_sharp) > abs(lam_chaser)


class TestConfidenceScaleTrackRecord:
    """confidence_scale использует max(bet_count, W+L)."""

    def setup_method(self):
        self.calc = BayesianScoreCalculator(
            prior_alpha=5.5,
            prior_beta=4.5,
            expert_weight_enabled=False,
        )

    def test_bet_count_zero_but_wl_present(self):
        """bet_count=0, W+L=75 → confidence_scale = 1.0 (не 0.1)."""
        inp_with_wl = {
            "S": 10000,
            "B_est": 100000,
            "b": 1.0,
            "W": 66,
            "L": 9,
            "direction": "buy",
            "current_price": 0.5,
            "bet_count": 0,
            "roi_percent": 10.0,
        }
        inp_no_wl = {**inp_with_wl, "W": 0, "L": 0}
        lam_with = self.calc.whale_bayes_factor(inp_with_wl)
        lam_without = self.calc.whale_bayes_factor(inp_no_wl)
        # Кит с 75 resolved markets должен давать более сильный сигнал
        assert abs(lam_with) > abs(lam_without) * 2


class TestProfileMultiplierFixes:
    """Тесты для fix-ов whale_profile_multiplier."""

    def test_experience_uses_wl_when_bet_count_zero(self):
        """bet_count=0, wins=50, losses=20 → experience bonus +0.15."""
        m = whale_profile_multiplier(
            roi_percent=10.0,
            bet_count=0,
            wins=50,
            losses=20,
        )
        # baseline=1.0, ROI +0.2, experience +0.15 = 1.35
        assert m == pytest.approx(1.35, abs=0.01)

    def test_experience_penalty_for_new_whale(self):
        """bet_count=0, wins=1, losses=0 → experience penalty -0.2."""
        m = whale_profile_multiplier(
            roi_percent=0.0,
            bet_count=0,
            wins=1,
            losses=0,
        )
        # baseline=1.0, ROI 0, experience -0.2 = 0.8
        assert m == pytest.approx(0.8, abs=0.01)

    def test_sector_boost_blocked_for_negative_roi(self):
        """ROI < 0 → sector win rate boost не применяется (даже при WR 90%)."""
        sector = {"crypto": {"wins": 45, "losses": 5}}
        m = whale_profile_multiplier(
            roi_percent=-5.0,
            bet_count=0,
            wins=45,
            losses=5,
            sector_win_rates=sector,
            market_category="crypto",
        )
        # baseline=1.0, ROI -0.1, experience +0.15, NO sector boost = 1.05
        assert m == pytest.approx(1.05, abs=0.01)

    def test_sector_boost_allowed_for_positive_roi(self):
        """ROI > 0 → sector boost применяется."""
        sector = {"crypto": {"wins": 45, "losses": 5}}
        m = whale_profile_multiplier(
            roi_percent=10.0,
            bet_count=0,
            wins=45,
            losses=5,
            sector_win_rates=sector,
            market_category="crypto",
        )
        # baseline=1.0, ROI +0.2, experience +0.15, sector +0.25 = 1.60
        assert m == pytest.approx(1.60, abs=0.01)
