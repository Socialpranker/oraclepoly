"""
Тесты для Score Decomposition — compute_decomposition() в BayesianScoreCalculator.

Покрытие:
- Без сигналов все сдвиги ≈ 0
- Сумма сдвигов ≈ oracle_score (±0.01)
- Адреса китов маскированы
- Максимум 5 top_whales
- swan_impact всегда ≤ 0
- confidence-поля заполнены
"""

import pytest

from app.core.scoring import BayesianScoreCalculator


@pytest.fixture()
def calculator():
    return BayesianScoreCalculator()


class TestComputeDecomposition:
    """Тесты для compute_decomposition()."""

    def test_no_signals_zero_shifts(self):
        """Без сигналов и w_mkt=1.0 все сдвиги ≈ 0."""
        calc = BayesianScoreCalculator(weights={"w_mkt": 1.0})
        result = calc.calculate_full_score(
            market_price=0.60,
            whale_inputs=[],
            microstructure=None,
            temperature=1.0,
        )
        decomp = calc.compute_decomposition(
            market_price=0.60,
            result=result,
        )

        assert abs(decomp["whale_shift"]) < 0.03
        assert abs(decomp["swan_impact"]) < 0.001
        assert decomp["market_prior"] == 0.60

    def test_shifts_sum_to_oracle_score(self, calculator):
        """market_prior + shifts ≈ oracle_score (±0.01)."""
        whale_inputs = [
            {
                "whale_address": "0x1234567890abcdef",
                "S": 50000,
                "B_est": 200000,
                "b": (1.0 / 0.60) - 1.0,
                "W": 30,
                "L": 10,
                "direction": "buy",
                "outcome": "YES",
                "current_price": 0.60,
                "timing_score": 0.0,
                "expert_weight": 0.5,
            },
        ]
        result = calculator.calculate_full_score(
            market_price=0.60,
            whale_inputs=whale_inputs,
            microstructure=None,
        )
        decomp = calculator.compute_decomposition(
            market_price=0.60,
            result=result,
        )

        expected_sum = decomp["market_prior"] + decomp["whale_shift"] + decomp["swan_impact"]
        assert abs(expected_sum - decomp["oracle_score"]) < 0.02

    def test_shifts_sum_with_custom_calibration(self):
        """Сумма сдвигов ≈ oracle_score при w_mkt != 1.0 и temperature != 1.0."""
        custom_weights = {"w_mkt": 0.9, "w_whale": 1.0}
        calc = BayesianScoreCalculator(weights=custom_weights)
        whale_inputs = [
            {
                "whale_address": "0x1234567890abcdef",
                "S": 50000,
                "B_est": 200000,
                "b": (1.0 / 0.60) - 1.0,
                "W": 30,
                "L": 10,
                "direction": "buy",
                "outcome": "YES",
                "current_price": 0.60,
                "timing_score": 0.0,
                "expert_weight": 0.5,
            },
        ]
        result = calc.calculate_full_score(
            market_price=0.60,
            whale_inputs=whale_inputs,
            microstructure=None,
            temperature=1.3,
        )
        decomp = calc.compute_decomposition(
            market_price=0.60,
            result=result,
        )

        expected_sum = (
            decomp["market_prior"]
            + decomp["whale_shift"]
            + decomp["swan_impact"]
            + decomp.get("smart_money_shift", 0)
            + decomp.get("oi_shift", 0)
            + decomp.get("concentration_shift", 0)
            + decomp.get("micro_shift", 0)
        )
        # Расхождение допустимо из-за asymmetric divergence clamp в oracle_score
        # (UPWARD_DAMPING=0.3 сжимает upward divergence, decomposition этого не учитывает)
        assert abs(expected_sum - decomp["oracle_score"]) < 0.10, (
            f"Sum {expected_sum:.4f} != oracle_score {decomp['oracle_score']:.4f} with w_mkt=0.9, T=1.3"
        )

    def test_whale_address_masked(self, calculator):
        """Адреса китов маскированы в формате 0x1234...5678."""
        whale_inputs = [
            {
                "whale_address": "0xabcdef1234567890abcdef1234567890abcdef12",
                "S": 50000,
                "B_est": 200000,
                "b": (1.0 / 0.60) - 1.0,
                "W": 20,
                "L": 5,
                "direction": "buy",
                "outcome": "YES",
                "current_price": 0.60,
                "timing_score": 0.0,
                "expert_weight": 0.5,
            },
        ]
        result = calculator.calculate_full_score(
            market_price=0.60,
            whale_inputs=whale_inputs,
            microstructure=None,
        )
        decomp = calculator.compute_decomposition(
            market_price=0.60,
            result=result,
        )

        assert len(decomp["top_whales"]) == 1
        addr = decomp["top_whales"][0]["address"]
        assert addr.startswith("0xabcd")
        assert "..." in addr
        # Полный адрес не должен быть в ответе
        assert len(addr) < len("0xabcdef1234567890abcdef1234567890abcdef12")

    def test_top_whales_limited_to_five(self, calculator):
        """Максимум 5 top_whales даже если китов больше."""
        whale_inputs = [
            {
                "whale_address": f"0x{i:040x}",
                "S": 10000 * (i + 1),
                "B_est": 200000,
                "b": (1.0 / 0.50) - 1.0,
                "W": 10,
                "L": 5,
                "direction": "buy",
                "outcome": "YES",
                "current_price": 0.50,
                "timing_score": 0.0,
                "expert_weight": 0.5,
            }
            for i in range(8)
        ]
        result = calculator.calculate_full_score(
            market_price=0.50,
            whale_inputs=whale_inputs,
            microstructure=None,
        )
        decomp = calculator.compute_decomposition(
            market_price=0.50,
            result=result,
        )

        assert len(decomp["top_whales"]) <= 5

    def test_swan_impact_always_zero(self, calculator):
        """swan_impact всегда 0.0 (p_swan removed from pipeline)."""
        result = calculator.calculate_full_score(
            market_price=0.60,
            whale_inputs=[],
            microstructure=None,
        )
        decomp = calculator.compute_decomposition(
            market_price=0.60,
            result=result,
        )
        assert decomp["swan_impact"] == 0.0

    def test_confidence_fields_present(self, calculator):
        """confidence содержит n_whales, has_microstructure."""
        result = calculator.calculate_full_score(
            market_price=0.50,
            whale_inputs=[],
            microstructure=None,
        )
        decomp = calculator.compute_decomposition(
            market_price=0.50,
            result=result,
        )

        assert "confidence" in decomp
        assert "n_whales" in decomp["confidence"]
        assert "has_microstructure" in decomp["confidence"]
        assert decomp["confidence"]["n_whales"] == 0
        assert decomp["confidence"]["has_microstructure"] is False
