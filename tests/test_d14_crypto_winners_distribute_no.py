"""D14 CryptoWinnersDistributeNO — unit tests."""

from __future__ import annotations

import pytest

from app.detectors import d14_crypto_winners_distribute_no as d14


def _event(**overrides):
    base = {
        "market_id": "0xmarket1",
        "category": "Crypto",
        "entry_price": 0.55,  # mid zone YES
        "hours_to_resolve": 6.0,  # within 4h band
        "smart_money_taker_share": 0.10,  # not used by D14 directly
        "taker_pnl_weighted_usd_24h": 100.0,  # winning takers
        "taker_roi_weighted_24h": 25.0,  # > 10%
        "buy_imbalance_asof": 0.30,  # < 0.45 (selling pressure)
        "n_takers_24h_before": 5,
    }
    base.update(overrides)
    return base


def test_emits_for_crypto_mid_4h_with_winning_sellers():
    sig = d14.detect(_event())
    assert sig is not None
    assert sig.side == "NO"
    assert sig.detector == "D14_crypto_winners_distribute_no"
    # current_price = YES canonical = 0.55 (NO entry cost = 1 - 0.55 = 0.45)
    assert sig.current_price == pytest.approx(0.55, abs=0.001)
    # Baseline WR = 0.602
    assert sig.confidence == pytest.approx(0.602, abs=0.001)
    assert sig.evidence["category"] == "Crypto"
    assert sig.evidence["taker_pnl_weighted_usd_24h"] == 100.0


def test_skip_non_crypto_category():
    """D14 only Crypto."""
    for cat in ["Politics", "Sports", "Tech & AI", "Other", "Finance", "Culture"]:
        assert d14.detect(_event(category=cat)) is None, f"Should skip {cat}"


def test_skip_zone_outside_mid():
    """Zone [0.50, 0.70) only."""
    assert d14.detect(_event(entry_price=0.49)) is None  # below floor
    assert d14.detect(_event(entry_price=0.70)) is None  # at ceiling (exclusive)
    assert d14.detect(_event(entry_price=0.85)) is None  # extreme


def test_emits_at_zone_boundary():
    """entry_price = 0.50 (exact floor) → emit."""
    sig = d14.detect(_event(entry_price=0.50))
    assert sig is not None


def test_skip_above_trigger_window():
    """Trigger window [4, 72]h (opened 2026-05-28) — режется только > 72h."""
    assert d14.detect(_event(hours_to_resolve=72.01)) is None
    assert d14.detect(_event(hours_to_resolve=96.0)) is None
    assert d14.detect(_event(hours_to_resolve=168.0)) is None


def test_skip_below_horizon():
    """< 4h gamma hell."""
    assert d14.detect(_event(hours_to_resolve=3.0)) is None
    assert d14.detect(_event(hours_to_resolve=1.0)) is None


def test_emits_across_trigger_window():
    """4h <= hours <= 72h → emit (full band, 4h-only пуст на текущем Polymarket)."""
    for h in [4.0, 6.0, 12.0, 24.0, 48.0, 72.0]:
        sig = d14.detect(_event(hours_to_resolve=h))
        assert sig is not None, f"Should emit для hours_to_resolve={h}"


def test_skip_buy_imbalance_above_threshold():
    """buy_imbalance >= 0.45 → skip (нет selling pressure)."""
    assert d14.detect(_event(buy_imbalance_asof=0.45)) is None
    assert d14.detect(_event(buy_imbalance_asof=0.50)) is None
    assert d14.detect(_event(buy_imbalance_asof=0.80)) is None


def test_skip_pnl_not_positive():
    """taker_pnl <= 0 (no winning takers) → skip."""
    assert d14.detect(_event(taker_pnl_weighted_usd_24h=0.0)) is None
    assert d14.detect(_event(taker_pnl_weighted_usd_24h=-50.0)) is None


def test_skip_taker_roi_too_low():
    """taker_roi <= 10 → skip (track record not strong enough)."""
    assert d14.detect(_event(taker_roi_weighted_24h=10.0)) is None
    assert d14.detect(_event(taker_roi_weighted_24h=5.0)) is None
    assert d14.detect(_event(taker_roi_weighted_24h=-20.0)) is None


def test_emits_at_taker_roi_threshold_just_above():
    sig = d14.detect(_event(taker_roi_weighted_24h=10.01))
    assert sig is not None


def test_skip_too_few_takers():
    """n_takers < 3 → skip."""
    assert d14.detect(_event(n_takers_24h_before=2)) is None
    assert d14.detect(_event(n_takers_24h_before=0)) is None


def test_skip_missing_required_fields():
    """Missing wallet quality data → skip (D14 strict)."""
    for missing_key in [
        "buy_imbalance_asof",
        "taker_pnl_weighted_usd_24h",
        "taker_roi_weighted_24h",
        "n_takers_24h_before",
    ]:
        e = _event()
        e.pop(missing_key)
        assert d14.detect(e) is None, f"Should skip missing {missing_key}"


def test_skip_no_market_id():
    assert d14.detect(_event(market_id=None)) is None
    assert d14.detect(_event(market_id="")) is None


def test_evidence_includes_all_inputs():
    sig = d14.detect(_event(taker_pnl_weighted_usd_24h=500.0, buy_imbalance_asof=0.20))
    assert sig is not None
    assert sig.evidence["taker_pnl_weighted_usd_24h"] == 500.0
    assert sig.evidence["buy_imbalance_asof"] == 0.20
    assert sig.evidence["taker_roi_weighted_24h"] == 25.0
    assert sig.evidence["n_takers_24h_before"] == 5


def test_invalid_field_types_treated_as_skip():
    """Non-numeric inputs → skip (gracefully)."""
    assert d14.detect(_event(buy_imbalance_asof="invalid")) is None
    assert d14.detect(_event(taker_pnl_weighted_usd_24h="bad")) is None


def test_horizon_sec_4h():
    sig = d14.detect(_event())
    assert sig.horizon_sec == 4 * 60 * 60


def test_expected_edge_pct_positive():
    """Edge должен быть positive in trigger window."""
    sig = d14.detect(_event())
    assert sig is not None
    # WR=0.602, payout=1/0.45=2.22, edge=(0.602*2.22 - 1)*100 = ~33%
    assert sig.expected_edge_pct > 25.0
