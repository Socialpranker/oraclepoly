"""Тесты чистых функций g18_shrinkage_replay (без БД). Аудит-задача 1.2."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts.research.g18_shrinkage_replay import (
    MIN_OBS_FOR_LOOKUP,
    brier_mean,
    brier_update,
    price_bin,
    run_walk_forward,
    shrink_wr,
    verdict_shrunk_vs_raw,
    week_start,
    wilson_midpoint,
)


class TestPriceBin:
    def test_floors_to_nearest_tenth(self):
        assert price_bin(0.35) == pytest.approx(0.3)
        assert price_bin(0.09) == pytest.approx(0.0)
        assert price_bin(0.99) == pytest.approx(0.9)

    def test_exact_boundary_stays_at_bin(self):
        assert price_bin(0.5) == pytest.approx(0.5)

    def test_price_one_clamped_to_last_bin(self):
        # floor(1.0*10)/10 = 1.0 без клампа — гарантируем что бин остаётся 0.9
        assert price_bin(1.0) == pytest.approx(0.9)

    def test_price_zero(self):
        assert price_bin(0.0) == pytest.approx(0.0)

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            price_bin(-0.1)
        with pytest.raises(ValueError):
            price_bin(1.1)


class TestShrinkWr:
    def test_large_n_dominates_prior(self):
        # n>>k → результат близок к wr_bin
        result = shrink_wr(n=1000, wr_bin=0.9, wr_prior=0.5, k=30)
        assert result == pytest.approx(0.9, abs=0.02)

    def test_small_n_pulled_to_prior(self):
        # n==k → ровно посередине между wr_bin и wr_prior
        result = shrink_wr(n=30, wr_bin=1.0, wr_prior=0.5, k=30)
        assert result == pytest.approx(0.75)

    def test_n_zero_returns_prior(self):
        assert shrink_wr(n=0, wr_bin=0.9, wr_prior=0.4, k=30) == pytest.approx(0.4)

    def test_k_zero_returns_wr_bin(self):
        assert shrink_wr(n=30, wr_bin=0.7, wr_prior=0.3, k=0) == pytest.approx(0.7)

    def test_larger_k_pulls_harder_to_prior(self):
        r_k30 = shrink_wr(n=30, wr_bin=1.0, wr_prior=0.5, k=30)
        r_k50 = shrink_wr(n=30, wr_bin=1.0, wr_prior=0.5, k=50)
        assert r_k50 < r_k30  # k=50 тянет сильнее к prior=0.5

    def test_negative_n_raises(self):
        with pytest.raises(ValueError):
            shrink_wr(n=-1, wr_bin=0.5, wr_prior=0.5, k=30)

    def test_negative_k_raises(self):
        with pytest.raises(ValueError):
            shrink_wr(n=10, wr_bin=0.5, wr_prior=0.5, k=-1)

    def test_both_zero_raises(self):
        with pytest.raises(ValueError):
            shrink_wr(n=0, wr_bin=0.5, wr_prior=0.5, k=0)


class TestWilsonMidpoint:
    def test_n_zero_returns_half(self):
        assert wilson_midpoint(0, 0) == pytest.approx(0.5)

    def test_large_n_converges_to_p_hat(self):
        result = wilson_midpoint(9000, 10000)
        assert result == pytest.approx(0.9, abs=0.01)

    def test_small_n_shrinks_toward_half(self):
        # 30/30 (p_hat=1.0) должен быть заметно ниже 1.0 — Wilson center регуляризует
        result = wilson_midpoint(30, 30)
        assert 0.7 < result < 1.0

    def test_wins_out_of_range_raises(self):
        with pytest.raises(ValueError):
            wilson_midpoint(-1, 10)
        with pytest.raises(ValueError):
            wilson_midpoint(11, 10)

    def test_bounded_zero_one(self):
        for wins, n in [(0, 30), (30, 30), (15, 30)]:
            result = wilson_midpoint(wins, n)
            assert 0.0 <= result <= 1.0


class TestBrierAccumulator:
    def test_perfect_prediction_zero_error(self):
        sq, n = brier_update(0.0, 0, pred=1.0, outcome=1)
        assert brier_mean(sq, n) == pytest.approx(0.0)

    def test_worst_prediction_max_error(self):
        sq, n = brier_update(0.0, 0, pred=1.0, outcome=0)
        assert brier_mean(sq, n) == pytest.approx(1.0)

    def test_accumulates_across_updates(self):
        sq, n = 0.0, 0
        sq, n = brier_update(sq, n, pred=0.5, outcome=1)  # error 0.25
        sq, n = brier_update(sq, n, pred=0.5, outcome=0)  # error 0.25
        assert brier_mean(sq, n) == pytest.approx(0.25)

    def test_empty_accumulator_returns_none(self):
        assert brier_mean(0.0, 0) is None

    def test_invalid_outcome_raises(self):
        with pytest.raises(ValueError):
            brier_update(0.0, 0, pred=0.5, outcome=2)

    def test_pred_out_of_range_raises(self):
        with pytest.raises(ValueError):
            brier_update(0.0, 0, pred=1.5, outcome=1)


class TestWeekStart:
    def test_monday_stays_same_day(self):
        monday = datetime(2026, 7, 6, 10, 30, tzinfo=UTC)  # a Monday
        result = week_start(monday)
        assert result == datetime(2026, 7, 6, 0, 0, tzinfo=UTC)

    def test_sunday_rolls_back_to_monday(self):
        sunday = datetime(2026, 7, 12, 23, 0, tzinfo=UTC)
        result = week_start(sunday)
        assert result == datetime(2026, 7, 6, 0, 0, tzinfo=UTC)

    def test_naive_datetime_assumed_utc(self):
        naive = datetime(2026, 7, 8, 12, 0)
        result = week_start(naive)
        assert result.tzinfo is not None


class TestVerdictShrunkVsRaw:
    def test_shrunk_better_when_lower_total_and_majority_weeks(self):
        verdict = verdict_shrunk_vs_raw(
            brier_raw_total=0.25, brier_variant_total=0.20, weekly_deltas=[-0.01, -0.02, 0.005]
        )
        assert verdict == "SHRUNK_BETTER"

    def test_raw_better_when_total_worse(self):
        verdict = verdict_shrunk_vs_raw(
            brier_raw_total=0.20, brier_variant_total=0.25, weekly_deltas=[-0.01, -0.02, -0.03]
        )
        assert verdict == "RAW_BETTER_OR_MIXED"

    def test_mixed_when_majority_of_weeks_worse(self):
        # Общий Brier ниже, но только 1/3 недель не хуже -> ниже порога 60%
        verdict = verdict_shrunk_vs_raw(
            brier_raw_total=0.25, brier_variant_total=0.24, weekly_deltas=[-0.01, 0.02, 0.03]
        )
        assert verdict == "RAW_BETTER_OR_MIXED"

    def test_exactly_60_percent_passes(self):
        # 3 из 5 недель не хуже == ровно 60%
        verdict = verdict_shrunk_vs_raw(
            brier_raw_total=0.25,
            brier_variant_total=0.20,
            weekly_deltas=[-0.01, -0.01, 0.0, 0.02, 0.03],
        )
        assert verdict == "SHRUNK_BETTER"

    def test_no_weekly_data_returns_no_data(self):
        assert verdict_shrunk_vs_raw(0.2, 0.2, []) == "NO_DATA"


class TestRunWalkForward:
    """Integration-style тест чистой walk-forward логики на синтетических сигналах (без БД)."""

    @staticmethod
    def _make_signal(detector: str, category: str, price: float, week_offset: int, won: int) -> dict:
        base = datetime(2026, 1, 5, tzinfo=UTC)  # Monday
        from datetime import timedelta

        return {
            "detector": detector,
            "category": category,
            "current_price": price,
            "generated_at": base + timedelta(weeks=week_offset, hours=1),
            "won": won,
        }

    def test_empty_signals_returns_empty(self):
        assert run_walk_forward([]) == {}

    def test_below_min_obs_never_evaluated(self):
        # Только 5 сигналов в train — ниже MIN_OBS_FOR_LOOKUP (30), eval-неделя должна быть пустой.
        signals = [self._make_signal("D14", "Crypto", 0.55, week_offset=0, won=i % 2) for i in range(5)]
        signals += [self._make_signal("D14", "Crypto", 0.55, week_offset=1, won=1)]
        result = run_walk_forward(signals)
        assert result == {}

    def test_above_min_obs_produces_metrics(self):
        # 40 train-сигналов недели 0 (одна цена, один бин) достаточно для MIN_OBS_FOR_LOOKUP=30.
        assert MIN_OBS_FOR_LOOKUP == 30
        train = [self._make_signal("D14", "Crypto", 0.55, week_offset=0, won=1 if i < 30 else 0) for i in range(40)]
        eval_week = [self._make_signal("D14", "Crypto", 0.55, week_offset=1, won=1)]
        result = run_walk_forward(train + eval_week)

        assert "D14" in result
        agg = result["D14"]
        assert agg["n_weeks"] == 1  # только week_offset=1 оценена (week 0 не имеет train до себя)
        assert agg["n_signals"] == 1
        raw_brier = brier_mean(*agg["acc"]["raw"])
        assert raw_brier is not None
        # wr_bin train = 30/40 = 0.75; outcome=1 → error = (0.75-1)^2 = 0.0625
        assert raw_brier == pytest.approx(0.0625, abs=1e-6)

    def test_leak_free_train_excludes_current_week(self):
        # Сигналы одной недели не должны попадать в свой же train-срез.
        same_week = [self._make_signal("D14", "Crypto", 0.55, week_offset=0, won=1) for _ in range(40)]
        result = run_walk_forward(same_week)
        # Единственная неделя не имеет предшествующего train -> ничего не оценено.
        assert result == {}
