"""Wilson score interval — proportion CI устойчивая к малым N и краевым p̂.

См. docs/superpowers/plans/2026-05-honest-baseline/01-wilson-ci.md.
"""

from __future__ import annotations

import math

import pytest

from app.services.wilson_ci import min_n_for_wilson_lower_bound, wilson_score_interval


class TestWilsonScoreInterval:
    def test_zero_total_returns_full_range(self):
        """N=0 → no information → [0, 1]."""
        lower, upper = wilson_score_interval(successes=0, total=0)
        assert lower == 0.0
        assert upper == 1.0

    def test_four_out_of_four_fills(self):
        """Classic case из ресёрча: 4/4 fills, 95% CI.

        Wald даёт [1.0, 1.0] — бесполезно.
        Wilson даёт [~0.51, 1.0] — содержательно."""
        lower, upper = wilson_score_interval(successes=4, total=4, confidence=0.95)
        assert 0.50 <= lower <= 0.55, f"expected ~0.51, got {lower}"
        assert upper == pytest.approx(1.0, abs=1e-6)

    def test_zero_successes_n_30(self):
        """0/30 fills, 95% — lower=0, upper показывает rule of three: ~3/N."""
        lower, upper = wilson_score_interval(successes=0, total=30, confidence=0.95)
        assert lower == pytest.approx(0.0, abs=1e-6)
        # Rule of three: upper ≈ 3/30 = 0.10, Wilson чуть консервативнее
        assert 0.08 <= upper <= 0.15

    def test_half_half_n_100(self):
        """50/100 → close to [0.40, 0.60] symmetric."""
        lower, upper = wilson_score_interval(successes=50, total=100, confidence=0.95)
        assert lower == pytest.approx(0.40, abs=0.02)
        assert upper == pytest.approx(0.60, abs=0.02)

    def test_n_22_at_p_hat_07_gives_lower_above_50(self):
        """Ресёрч claim: при p̂=0.7, N=22 → Wilson lower > 0.50.

        Реальность: 22 × 0.7 = 15.4. Берём 15 (15/22 = 0.682) и 16 (16/22 = 0.727).
        15/22 даёт lower ≈ 0.476 < 0.50 (ресёрч был оптимистичен).
        16/22 даёт lower > 0.50.
        """
        lower_16, _ = wilson_score_interval(successes=16, total=22, confidence=0.95)
        assert lower_16 > 0.50, f"expected lower > 50%, got {lower_16:.4f}"

    def test_confidence_99_wider_than_95(self):
        l95, u95 = wilson_score_interval(50, 100, 0.95)
        l99, u99 = wilson_score_interval(50, 100, 0.99)
        assert l99 < l95
        assert u99 > u95

    def test_bounds_always_in_zero_one(self):
        """Wilson never escapes [0, 1] (unlike Wald)."""
        for s, n in [(0, 5), (5, 5), (1, 100), (99, 100), (10, 10)]:
            lower, upper = wilson_score_interval(s, n, 0.95)
            assert 0.0 <= lower <= 1.0, f"lower out of bounds for ({s},{n}): {lower}"
            assert 0.0 <= upper <= 1.0, f"upper out of bounds for ({s},{n}): {upper}"
            assert lower <= upper

    def test_d36_obi_pressure_realistic(self):
        """Real-world spot check: D36 113/113 fills → даже при 100% Wilson сохраняет honest CI."""
        lower, upper = wilson_score_interval(113, 113, 0.95)
        # При 113/113 lower должен быть высоким (~0.97), не 1.0
        assert 0.95 <= lower <= 0.99, f"D36 Wilson lower: {lower:.4f}"
        assert upper == pytest.approx(1.0, abs=1e-6)

    def test_z_score_approx_for_95(self):
        """Sanity: при N→inf и p=0.5 margin → z×sqrt(0.25/N) ≈ 1.96 × 0.5/√N.

        N=10000, p=0.5 → margin ≈ 0.0098. Verify within tolerance."""
        lower, upper = wilson_score_interval(5000, 10000, 0.95)
        margin = (upper - lower) / 2
        # Expected: ~0.0098 ± 0.001
        assert 0.008 <= margin <= 0.012

    def test_invalid_successes_negative(self):
        with pytest.raises(ValueError):
            wilson_score_interval(successes=-1, total=10)

    def test_invalid_successes_above_total(self):
        with pytest.raises(ValueError):
            wilson_score_interval(successes=5, total=3)

    def test_invalid_confidence_above_one(self):
        with pytest.raises(ValueError):
            wilson_score_interval(successes=5, total=10, confidence=1.5)

    def test_invalid_confidence_zero(self):
        with pytest.raises(ValueError):
            wilson_score_interval(successes=5, total=10, confidence=0.0)

    def test_invalid_total_negative(self):
        with pytest.raises(ValueError):
            wilson_score_interval(successes=0, total=-1)


class TestMinNForWilsonLowerBound:
    def test_p_hat_1_target_0_5(self):
        """При p̂=1.0 и target lower > 0.50: N=4 хватает (4/4 → 51%)."""
        n = min_n_for_wilson_lower_bound(p_hat=1.0, lower_threshold=0.50, confidence=0.95)
        assert n == 4

    def test_p_hat_07_target_0_5(self):
        """При p̂=0.7 нужно ~22-23 (round(0.7×N) даёт integer count).

        Точное N зависит от rounding. Tolerance ±5."""
        n = min_n_for_wilson_lower_bound(p_hat=0.7, lower_threshold=0.50, confidence=0.95)
        assert 18 <= n <= 28, f"expected 18-28, got {n}"

    def test_p_hat_055_needs_large_n(self):
        """При p̂=0.55, threshold=0.50: marginal observation, нужно много данных.

        Note: ресёрч утверждал ~165, но прямой расчёт даёт ~370. Wilson lower
        приближается к threshold очень медленно при p̂ близком к threshold.
        Это reinforce исходную motivation Stage 3 (требовать жёсткие N gates).
        """
        n = min_n_for_wilson_lower_bound(p_hat=0.55, lower_threshold=0.50, confidence=0.95)
        # Real value ≈ 370. Tolerance ±50.
        assert 320 <= n <= 420, f"expected 320-420, got {n}"

    def test_unreachable_target_raises(self):
        """p̂ ≤ threshold → невозможно никаким N."""
        with pytest.raises(ValueError):
            min_n_for_wilson_lower_bound(p_hat=0.30, lower_threshold=0.50)

    def test_p_hat_equals_threshold_raises(self):
        """Edge: p̂ == threshold → CI lower всегда < threshold по конструкции."""
        with pytest.raises(ValueError):
            min_n_for_wilson_lower_bound(p_hat=0.50, lower_threshold=0.50)

    def test_p_hat_out_of_range_raises(self):
        with pytest.raises(ValueError):
            min_n_for_wilson_lower_bound(p_hat=1.5, lower_threshold=0.50)
        with pytest.raises(ValueError):
            min_n_for_wilson_lower_bound(p_hat=-0.1, lower_threshold=0.50)

    def test_threshold_out_of_range_raises(self):
        with pytest.raises(ValueError):
            min_n_for_wilson_lower_bound(p_hat=0.7, lower_threshold=1.5)
        with pytest.raises(ValueError):
            min_n_for_wilson_lower_bound(p_hat=0.7, lower_threshold=0.0)


class TestInternalErfHelper:
    """Sanity test для inverse error function approximation."""

    def test_inv_erf_z_for_95_confidence(self):
        """z для 95% two-sided ≈ 1.96."""
        from app.services.wilson_ci import _z_score

        z = _z_score(0.95)
        assert z == pytest.approx(1.96, abs=0.005)

    def test_inv_erf_z_for_99_confidence(self):
        """z для 99% ≈ 2.576."""
        from app.services.wilson_ci import _z_score

        z = _z_score(0.99)
        assert z == pytest.approx(2.576, abs=0.005)

    def test_inv_erf_z_for_90_confidence(self):
        """z для 90% ≈ 1.645."""
        from app.services.wilson_ci import _z_score

        z = _z_score(0.90)
        assert z == pytest.approx(1.645, abs=0.005)

    def test_erf_round_trip(self):
        """erf(erfinv(x)) ≈ x в нашем диапазоне."""
        from app.services.wilson_ci import _inv_erf

        for x in [-0.9, -0.5, 0.0, 0.5, 0.9]:
            y = _inv_erf(x)
            # Compare to math.erf(y) ≈ x
            assert math.erf(y) == pytest.approx(x, abs=1e-3)
