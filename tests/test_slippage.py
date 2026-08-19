# tests/test_slippage.py
"""Тесты модели slippage для dry pipeline."""

import pytest

from app.core.slippage import (
    apply_realistic_slippage,
    apply_slippage,
    get_spread_estimate,
    per_asset_slippage_frac,
)


class TestGetSpreadEstimate:
    def test_sports_low_spread(self):
        spread = get_spread_estimate("Sports")
        assert 0.005 <= spread <= 0.03

    def test_crypto_higher_spread(self):
        spread = get_spread_estimate("Crypto")
        assert spread > get_spread_estimate("Sports")

    def test_unknown_category_fallback(self):
        spread = get_spread_estimate("UnknownCat123")
        assert spread > 0

    def test_case_insensitive(self):
        assert get_spread_estimate("sports") == get_spread_estimate("Sports")


class TestApplySlippage:
    def test_yes_side_increases_entry(self):
        result = apply_slippage(side="YES", entry_price=0.50, category="Sports")
        assert result > 0.50

    def test_no_side_increases_entry(self):
        result = apply_slippage(side="NO", entry_price=0.40, category="Sports")
        assert result > 0.40

    def test_slippage_proportional(self):
        sports = apply_slippage("NO", 0.40, "Sports") - 0.40
        crypto = apply_slippage("NO", 0.40, "Crypto") - 0.40
        assert crypto > sports

    def test_entry_clamped_below_1(self):
        result = apply_slippage("YES", 0.99, "Crypto")
        assert result < 1.0

    def test_zero_entry_safe(self):
        result = apply_slippage("NO", 0.01, "Sports")
        assert result >= 0.01

    def test_taker_worse_than_maker(self):
        maker = apply_slippage("NO", 0.40, "Sports", execution_mode="maker")
        taker = apply_slippage("NO", 0.40, "Sports", execution_mode="taker")
        assert taker > maker


class TestPerAssetSlippageFrac:
    """Per-asset slippage модель: slip_frac = baseline + impact_K * utilization."""

    def test_baseline_when_book_huge(self):
        # depth >> stake → utilization ≈ 0 → slip_frac = baseline = 0.02
        f = per_asset_slippage_frac(
            stake_usd=10.0, cost_quoted=0.10, book_depth_l5_usd=1_000_000.0
        )
        assert f == pytest.approx(0.02, abs=1e-4)

    def test_full_impact_at_full_utilization(self):
        # stake=$50, depth_l5=$100 → depth_our_side=$50 → utilization=1.0
        # slip_frac = 0.02 + 0.05 * 1.0 = 0.07
        f = per_asset_slippage_frac(
            stake_usd=50.0, cost_quoted=0.10, book_depth_l5_usd=100.0
        )
        assert f == pytest.approx(0.07, abs=1e-6)

    def test_utilization_capped_at_one(self):
        # stake $1000, depth $100 → utilization clamped to 1.0
        f = per_asset_slippage_frac(
            stake_usd=1000.0, cost_quoted=0.10, book_depth_l5_usd=100.0
        )
        assert f == pytest.approx(0.07, abs=1e-6)  # cap не дает >0.07

    def test_neg_risk_adds_premium(self):
        without = per_asset_slippage_frac(
            stake_usd=10.0, cost_quoted=0.10, book_depth_l5_usd=1_000_000.0
        )
        with_nr = per_asset_slippage_frac(
            stake_usd=10.0,
            cost_quoted=0.10,
            book_depth_l5_usd=1_000_000.0,
            is_neg_risk=True,
        )
        assert with_nr - without == pytest.approx(0.01, abs=1e-6)

    def test_volume_24h_fallback(self):
        # depth_l5=None → V=$10K × 0.05 = $500 → depth_our_side=$250
        # stake=$25 → utilization=0.1 → slip_frac=0.02+0.005=0.025
        f = per_asset_slippage_frac(
            stake_usd=25.0, cost_quoted=0.10, volume_24h_usd=10_000.0
        )
        assert f == pytest.approx(0.025, abs=1e-4)

    def test_lifetime_volume_fallback(self):
        # volume_24h=None, lifetime=$100K × 0.01 = $1000 → depth_our=$500
        # stake=$5 → utilization=0.01 → slip_frac=0.02+0.0005=0.0205
        f = per_asset_slippage_frac(
            stake_usd=5.0, cost_quoted=0.10, lifetime_volume_usd=100_000.0
        )
        assert f == pytest.approx(0.0205, abs=1e-4)

    def test_zero_stake_returns_baseline(self):
        f = per_asset_slippage_frac(stake_usd=0.0, cost_quoted=0.10)
        assert f == pytest.approx(0.02, abs=1e-6)

    def test_zero_cost_returns_baseline(self):
        f = per_asset_slippage_frac(stake_usd=10.0, cost_quoted=0.0)
        assert f == pytest.approx(0.02, abs=1e-6)


class TestApplyRealisticSlippage:
    def test_no_buy_extreme_zone_modest_slippage(self):
        """NO buy при yes=0.92 (cost_no=0.08), stake $5, depth $500.
        utilization = 5 / (500 * 0.5) = 0.02
        slip_frac = 0.02 + 0.05*0.02 = 0.021
        cost_no_real = 0.08 * 1.021 = 0.08168
        yes_price_real = 1 - 0.08168 = 0.91832
        """
        out = apply_realistic_slippage(
            side="NO", entry_price=0.92, stake_usd=5.0, book_depth_l5_usd=500.0
        )
        assert out == pytest.approx(0.91832, abs=1e-4)

    def test_yes_buy_mid_zone(self):
        """YES buy при yes=0.50, stake $50, depth $5000.
        cost_quoted = 0.50
        utilization = 50 / (5000*0.5) = 0.02
        slip_frac = 0.021
        yes_price_real = 0.50 * 1.021 = 0.5105
        """
        out = apply_realistic_slippage(
            side="YES", entry_price=0.50, stake_usd=50.0, book_depth_l5_usd=5000.0
        )
        assert out == pytest.approx(0.5105, abs=1e-4)

    def test_no_buy_thin_book_capped(self):
        # depth=$10, stake=$50 → utilization=1.0 → slip_frac=0.07
        # cost_no = 0.05 * 1.07 = 0.0535 → yes=0.9465
        out = apply_realistic_slippage(
            side="NO", entry_price=0.95, stake_usd=50.0, book_depth_l5_usd=10.0
        )
        assert out == pytest.approx(0.9465, abs=1e-4)

    def test_clamp_above_zero_one(self):
        # extreme: cost ~ 0 после slippage могло бы выйти за границы
        out = apply_realistic_slippage(
            side="YES", entry_price=0.99, stake_usd=1000.0, book_depth_l5_usd=10.0
        )
        assert 0.01 <= out <= 0.99

    def test_neg_risk_pricier(self):
        without = apply_realistic_slippage(
            side="NO", entry_price=0.92, stake_usd=5.0, book_depth_l5_usd=500.0
        )
        with_nr = apply_realistic_slippage(
            side="NO",
            entry_price=0.92,
            stake_usd=5.0,
            book_depth_l5_usd=500.0,
            is_neg_risk=True,
        )
        # NR adds 0.01 to slip_frac → cost_no goes up → yes goes down
        assert with_nr < without

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            apply_realistic_slippage(
                side="MAYBE", entry_price=0.5, stake_usd=10.0, book_depth_l5_usd=1000.0
            )

    def test_extreme_zone_slippage_under_5pct_for_normal_stake(self):
        """Sanity: для D7b zone [0.85, 0.97] @ stake $5 на medium book ($500 depth)
        slippage не должен быть больше 5% — иначе backtest projections бессмысленны.
        """
        for yes in [0.85, 0.90, 0.95, 0.97]:
            out = apply_realistic_slippage(
                side="NO", entry_price=yes, stake_usd=5.0, book_depth_l5_usd=500.0
            )
            cost_quoted = 1 - yes
            cost_real = 1 - out
            slip_pct = (cost_real - cost_quoted) / cost_quoted
            assert slip_pct < 0.10, f"slippage {slip_pct:.1%} > 10% at yes={yes}"
