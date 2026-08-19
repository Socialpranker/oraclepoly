# tests/test_fee_calculator.py
"""Тесты fee_calculator — Polymarket fee schedule (2026-03-30)."""

import pytest

from app.services.fee_calculator import FeeBreakdown, calculate, estimate_trading_fee


class TestTradingFeeFormula:
    """Проверка формулы: amount × feeRate × (p × (1-p))^exponent."""

    def test_crypto_peak(self):
        """Crypto при p=0.5: 100 × 0.072 × 0.25 = 1.80 (peak 1.80%)."""
        result = calculate(100.0, 0.50, "crypto", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.80, abs=0.01)

    def test_sports_peak(self):
        """Sports при p=0.5: 100 × 0.03 × 0.25 = 0.75 (peak 0.75%)."""
        result = calculate(100.0, 0.50, "sports", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(0.75, abs=0.01)

    def test_politics_peak(self):
        """Politics при p=0.5: 100 × 0.04 × 0.25 = 1.00 (peak 1.00%)."""
        result = calculate(100.0, 0.50, "politics", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.00, abs=0.01)

    def test_finance_peak(self):
        """Finance при p=0.5: 100 × 0.04 × 0.25 = 1.00."""
        result = calculate(100.0, 0.50, "finance", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.00, abs=0.01)

    def test_culture_peak(self):
        """Culture при p=0.5: 100 × 0.05 × 0.25 = 1.25."""
        result = calculate(100.0, 0.50, "culture", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.25, abs=0.01)

    def test_tech_peak(self):
        """Tech при p=0.5: 100 × 0.04 × 0.25 = 1.00."""
        result = calculate(100.0, 0.50, "tech", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.00, abs=0.01)

    def test_economics_peak(self):
        """Economics (exponent=1): 100 × 0.05 × 0.25 = 1.25 (peak 1.25%)."""
        result = calculate(100.0, 0.50, "economics", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.25, abs=0.01)

    def test_weather_peak(self):
        """Weather (exponent=1): 100 × 0.05 × 0.25 = 1.25 (peak 1.25%)."""
        result = calculate(100.0, 0.50, "weather", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.25, abs=0.01)

    def test_other_peak(self):
        """Other (exponent=1): 100 × 0.05 × 0.25 = 1.25 (peak 1.25%)."""
        result = calculate(100.0, 0.50, "other", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.25, abs=0.01)

    def test_mentions_peak(self):
        """Mentions (exponent=1): 100 × 0.04 × 0.25 = 1.00 (peak 1.00%)."""
        result = calculate(100.0, 0.50, "mentions", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.00, abs=0.01)


class TestGeopoliticsFree:
    """Geopolitics — fee-free категория."""

    def test_geopolitics_zero_fee(self):
        result = calculate(100.0, 0.50, "geopolitics", "direct", 7.0, gas_per_trade=0.0)
        assert result.trading_fee == 0.0

    def test_world_economy_maps_to_geopolitics(self):
        """Наша категория 'World & Economy' → geopolitics (fee-free)."""
        result = calculate(100.0, 0.50, "World & Economy", "direct", 7.0, gas_per_trade=0.0)
        assert result.trading_fee == 0.0


class TestCategoryMapping:
    """Маппинг наших категорий → Polymarket fee schedule."""

    def test_tech_ai_maps_to_tech(self):
        """'Tech & AI' → 'tech' (rate 0.04, exp 1)."""
        result = calculate(100.0, 0.50, "Tech & AI", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.00, abs=0.01)

    def test_case_insensitive(self):
        """Категории case-insensitive."""
        r1 = calculate(100.0, 0.50, "CRYPTO", "direct", 0.0, gas_per_trade=0.0)
        r2 = calculate(100.0, 0.50, "crypto", "direct", 0.0, gas_per_trade=0.0)
        assert r1.trading_fee == r2.trading_fee

    def test_unknown_category_uses_default(self):
        """Неизвестная категория → default (0.05, 1) → peak 1.25%."""
        result = calculate(100.0, 0.50, "unknown_cat", "direct", 0.0, gas_per_trade=0.0)
        assert result.trading_fee == pytest.approx(1.25, abs=0.01)


class TestFeeAtExtremes:
    """Комиссии на крайних вероятностях (p ≈ 0 или p ≈ 1)."""

    def test_fee_near_zero_probability(self):
        """При p=0.05 комиссия минимальна."""
        result = calculate(100.0, 0.05, "crypto", "direct", 0.0, gas_per_trade=0.0)
        # 100 × 0.072 × (0.05 × 0.95)^1 = 7.2 × 0.0475 = 0.342
        assert result.trading_fee == pytest.approx(0.342, abs=0.01)

    def test_fee_near_one_probability(self):
        """При p=0.95 комиссия минимальна (симметрично)."""
        r_low = calculate(100.0, 0.05, "crypto", "direct", 0.0, gas_per_trade=0.0)
        r_high = calculate(100.0, 0.95, "crypto", "direct", 0.0, gas_per_trade=0.0)
        assert r_low.trading_fee == pytest.approx(r_high.trading_fee, abs=0.001)

    def test_economics_off_peak(self):
        """Economics (exp=1): fee при p=0.10: 100 × 0.05 × (0.1 × 0.9) = 0.45."""
        econ = calculate(100.0, 0.10, "economics", "direct", 0.0, gas_per_trade=0.0)
        assert econ.trading_fee == pytest.approx(0.45, abs=0.01)


class TestProfiles:
    """Профили комиссий: free, direct, global."""

    def test_free_profile_zero(self):
        result = calculate(100.0, 0.50, "crypto", "free", 7.0)
        assert result == FeeBreakdown(0.0, 0.0, 0.0, 0.0)

    def test_direct_profile_full(self):
        result = calculate(100.0, 0.50, "crypto", "direct", 30.0)
        assert result.trading_fee == pytest.approx(1.80, abs=0.01)
        assert result.gas_fee == 0.0  # субсидирован Polymarket Relayer
        # opp_cost = 100 × 0.05 × (30/365) ≈ 0.411
        assert result.opportunity_cost == pytest.approx(0.411, abs=0.01)
        assert result.total == pytest.approx(result.trading_fee + result.gas_fee + result.opportunity_cost, abs=0.01)

    def test_global_same_trading_as_direct(self):
        direct = calculate(100.0, 0.50, "crypto", "direct", 7.0)
        global_ = calculate(100.0, 0.50, "crypto", "global", 7.0)
        assert direct.trading_fee == global_.trading_fee

    def test_zero_days_no_opp_cost(self):
        result = calculate(100.0, 0.50, "crypto", "direct", 0.0)
        assert result.opportunity_cost == 0.0

    def test_custom_gas_and_rate(self):
        result = calculate(100.0, 0.50, "crypto", "direct", 365.0, gas_per_trade=0.05, annual_rate=0.10)
        assert result.gas_fee == 0.05
        assert result.opportunity_cost == pytest.approx(10.0, abs=0.01)


class TestPerMarketFeeSchedule:
    """Per-market feeSchedule из Gamma API.

    Когда переданы реальные параметры markets.fee_schedule — используем их,
    игнорируя категорию. Категория продолжает быть fallback'ом если schedule
    отсутствует или невалиден.
    """

    def test_uses_schedule_rate_when_provided(self):
        """Категория говорит crypto (0.072), но Gamma вернул для маркета 0.05."""
        result = estimate_trading_fee(
            100.0, 0.50, "crypto", fee_schedule={"rate": 0.05, "exponent": 1}
        )
        # 100 × 0.05 × 0.25 = 1.25 (не 1.80 как для crypto)
        assert result == pytest.approx(1.25, abs=0.01)

    def test_uses_schedule_exponent(self):
        """Custom exponent применяется поверх rate."""
        result = estimate_trading_fee(
            100.0, 0.50, "politics", fee_schedule={"rate": 0.04, "exponent": 2}
        )
        # 100 × 0.04 × (0.25)^2 = 0.25
        assert result == pytest.approx(0.25, abs=0.01)

    def test_falls_back_to_category_if_schedule_none(self):
        """fee_schedule=None → используем категорийную таблицу."""
        result = estimate_trading_fee(100.0, 0.50, "crypto", fee_schedule=None)
        assert result == pytest.approx(1.80, abs=0.01)

    def test_falls_back_to_category_if_schedule_missing_rate(self):
        """fee_schedule без rate → невалидно → fallback на категорию."""
        result = estimate_trading_fee(
            100.0, 0.50, "crypto", fee_schedule={"exponent": 1, "takerOnly": True}
        )
        assert result == pytest.approx(1.80, abs=0.01)

    def test_zero_rate_in_schedule_returns_zero(self):
        """rate=0 — валидно (fee-free market), не fallback."""
        result = estimate_trading_fee(
            100.0, 0.50, "crypto", fee_schedule={"rate": 0.0, "exponent": 1}
        )
        assert result == 0.0

    def test_invalid_exponent_defaults_to_one(self):
        """exponent ≤ 0 невалиден → используем 1."""
        result = estimate_trading_fee(
            100.0, 0.50, "politics", fee_schedule={"rate": 0.04, "exponent": 0}
        )
        # exponent игнорируем как 1: 100 × 0.04 × 0.25 = 1.00
        assert result == pytest.approx(1.00, abs=0.01)

    def test_calculate_passes_schedule_through(self):
        """calculate() прокидывает fee_schedule в _trading_fee."""
        result = calculate(
            100.0,
            0.50,
            "crypto",
            "direct",
            0.0,
            fee_schedule={"rate": 0.03, "exponent": 1},
        )
        # 100 × 0.03 × 0.25 = 0.75
        assert result.trading_fee == pytest.approx(0.75, abs=0.01)

    def test_real_gamma_sample_general_fees(self):
        """Реальный sample из Gamma (general_fees, observed 2026-05-21):
        {rate: 0.05, exponent: 1, takerOnly: true, rebateRate: 0.25}.
        Peak fee 1.25%."""
        result = estimate_trading_fee(
            1000.0,
            0.50,
            "Other",
            fee_schedule={"rate": 0.05, "exponent": 1, "takerOnly": True, "rebateRate": 0.25},
        )
        # 1000 × 0.05 × 0.25 = 12.5
        assert result == pytest.approx(12.5, abs=0.01)


class TestMakerRebate:
    """Maker rebate — taker_fee × (1 - rebate_rate).

    По умолчанию estimate_trading_fee считает taker fee (worst case).
    is_maker=True применяет rebate из feeSchedule.rebateRate или категории.
    """

    def test_taker_default(self):
        """Без is_maker — taker fee (как было раньше)."""
        result = estimate_trading_fee(100.0, 0.50, "politics")
        # 100 × 0.04 × 0.25 = 1.00
        assert result == pytest.approx(1.00, abs=0.01)

    def test_maker_uses_category_rebate_default(self):
        """is_maker=True, без schedule → rebate из категорийной таблицы (politics=0.25)."""
        result = estimate_trading_fee(100.0, 0.50, "politics", is_maker=True)
        # taker=1.00, maker = 1.00 × (1 - 0.25) = 0.75
        assert result == pytest.approx(0.75, abs=0.01)

    def test_maker_uses_schedule_rebate(self):
        """is_maker=True + schedule.rebateRate → используется schedule (overrides category)."""
        result = estimate_trading_fee(
            100.0,
            0.50,
            "politics",
            fee_schedule={"rate": 0.04, "rebateRate": 0.40},
            is_maker=True,
        )
        # taker = 100 × 0.04 × 0.25 = 1.00, maker = 1.00 × (1 - 0.40) = 0.60
        assert result == pytest.approx(0.60, abs=0.01)

    def test_crypto_maker_rebate_20pct(self):
        """Crypto имеет нестандартный rebate 20% (vs 25% у остальных)."""
        result = estimate_trading_fee(100.0, 0.50, "crypto", is_maker=True)
        # taker = 100 × 0.072 × 0.25 = 1.80, maker = 1.80 × (1 - 0.20) = 1.44
        assert result == pytest.approx(1.44, abs=0.01)

    def test_geopolitics_maker_zero_fee_zero_rebate(self):
        """Geopolitics fee-free → 0 в обе стороны (taker и maker)."""
        result = estimate_trading_fee(100.0, 0.50, "geopolitics", is_maker=True)
        assert result == 0.0

    def test_calculate_passes_is_maker(self):
        """calculate() прокидывает is_maker в _trading_fee."""
        taker = calculate(100.0, 0.50, "politics", "direct", 0.0, is_maker=False)
        maker = calculate(100.0, 0.50, "politics", "direct", 0.0, is_maker=True)
        assert taker.trading_fee == pytest.approx(1.00, abs=0.01)
        assert maker.trading_fee == pytest.approx(0.75, abs=0.01)

    def test_invalid_rebate_falls_back_to_category(self):
        """rebateRate out-of-range (>1.0) — игнорируем schedule, fallback на категорию."""
        result = estimate_trading_fee(
            100.0,
            0.50,
            "politics",
            fee_schedule={"rate": 0.04, "rebateRate": 1.5},
            is_maker=True,
        )
        # Fallback на politics category rebate = 0.25
        # taker = 1.00, maker = 0.75
        assert result == pytest.approx(0.75, abs=0.01)


