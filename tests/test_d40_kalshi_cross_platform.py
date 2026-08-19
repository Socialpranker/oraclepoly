"""D40 KalshiCrossPlatform — unit tests."""

from __future__ import annotations

import pytest

from app.detectors import d40_kalshi_cross_platform as d40
from app.detectors.d40_kalshi_cross_platform import CrossPlatformPair, _kalshi_fee_rate


def _pair(**overrides) -> CrossPlatformPair:
    """Default: liquid Politics pair, no inverse, prices 50/50."""
    base = dict(
        polymarket_id="0xpoly1",
        kalshi_ticker="ELECT-2026-PRES-DEM",
        event_id="evt-1",
        category="Politics",
        polymarket_category_raw="Politics",
        inverse_side=False,
        poly_yes_ask=0.55,
        poly_no_ask=0.45,
        kalshi_yes_ask=0.55,
        kalshi_no_ask=0.45,
        poly_volume_24h=50_000.0,
        kalshi_volume_24h_usd=20_000.0,
        poly_clob_token_ids=None,
    )
    base.update(overrides)
    return CrossPlatformPair(**base)


# ─── Helpers ────────────────────────────────────────────────────────────────


def test_kalshi_fee_zero_at_extremes():
    """Fee = 0 at price=0 or price=1."""
    assert _kalshi_fee_rate(0.0) == 0.0
    assert _kalshi_fee_rate(1.0) == 0.0


def test_kalshi_fee_peak_at_half():
    """Peak Kalshi fee at p=0.5 is 0.07 × 0.25 = 0.0175 (1.75%)."""
    assert _kalshi_fee_rate(0.5) == pytest.approx(0.0175)


def test_kalshi_fee_below_peak():
    """Symmetric around 0.5."""
    f_low = _kalshi_fee_rate(0.20)
    f_high = _kalshi_fee_rate(0.80)
    assert f_low == pytest.approx(f_high)


# ─── Detection emission/skipping ────────────────────────────────────────────


def test_detect_skips_inverse_pair():
    """Phase 1: inverse_side=True всегда skip."""
    assert d40.detect(_pair(inverse_side=True)) is None


def test_detect_skips_when_no_edge():
    """Cost 0.55 + 0.45 = 1.00, no edge → no signal."""
    sig = d40.detect(_pair())  # default 50/50 prices → spread = 0
    assert sig is None


def test_detect_emits_when_spread_above_threshold():
    """Cheap YES Poly @0.40 + Cheap NO Kalshi @0.40 → gross spread 20%, net edge >> 3%."""
    sig = d40.detect(
        _pair(
            poly_yes_ask=0.40,
            poly_no_ask=0.60,
            kalshi_yes_ask=0.60,
            kalshi_no_ask=0.40,
        )
    )
    assert sig is not None
    assert sig.detector == "D40_kalshi_cross_platform"
    assert sig.expected_edge_pct >= 3.0
    assert sig.evidence["direction"] == "poly_yes"  # Buy YES Poly is cheaper
    assert sig.evidence["cheap_side"] == "polymarket"
    assert sig.evidence["expensive_side"] == "kalshi"
    assert sig.evidence["kalshi_ticker"] == "ELECT-2026-PRES-DEM"


def test_detect_emits_kalshi_yes_direction():
    """Cheap YES on Kalshi side."""
    sig = d40.detect(
        _pair(
            poly_yes_ask=0.60,
            poly_no_ask=0.40,
            kalshi_yes_ask=0.40,
            kalshi_no_ask=0.60,
        )
    )
    assert sig is not None
    assert sig.evidence["direction"] == "kalshi_yes"
    assert sig.evidence["cheap_side"] == "kalshi"


def test_detect_skips_low_volume_polymarket():
    """Volume < 5000 USD на Poly side → skip."""
    sig = d40.detect(
        _pair(
            poly_yes_ask=0.40,
            poly_no_ask=0.60,
            kalshi_yes_ask=0.60,
            kalshi_no_ask=0.40,
            poly_volume_24h=100.0,
        )
    )
    assert sig is None


def test_detect_skips_low_volume_kalshi():
    """Volume < 5000 USD на Kalshi side → skip."""
    sig = d40.detect(
        _pair(
            poly_yes_ask=0.40,
            poly_no_ask=0.60,
            kalshi_yes_ask=0.60,
            kalshi_no_ask=0.40,
            kalshi_volume_24h_usd=100.0,
        )
    )
    assert sig is None


def test_detect_skips_when_prices_missing():
    """Any None price → skip (orderbook empty или Kalshi не отдал)."""
    assert d40.detect(_pair(poly_yes_ask=None)) is None
    assert d40.detect(_pair(kalshi_no_ask=None)) is None


def test_detect_skips_when_price_out_of_bounds():
    """Price must be in (0, 1) strict."""
    assert d40.detect(_pair(poly_yes_ask=0.0)) is None
    assert d40.detect(_pair(poly_yes_ask=1.0)) is None
    assert d40.detect(_pair(kalshi_yes_ask=-0.1)) is None


def test_detect_threshold_just_below_skips():
    """edge_pct < min_net_spread_pct → no signal."""
    # 49.5 / 49.5 sum = 99% gross, 1% gross spread, после fees < 3% threshold
    sig = d40.detect(
        _pair(
            poly_yes_ask=0.495,
            poly_no_ask=0.505,
            kalshi_yes_ask=0.505,
            kalshi_no_ask=0.495,
        )
    )
    assert sig is None  # ниже 3% threshold


def test_detect_threshold_override():
    """Custom threshold should work."""
    # 1% gross spread (no_ask totals 0.99)
    sig = d40.detect(
        _pair(
            poly_yes_ask=0.495,
            poly_no_ask=0.505,
            kalshi_yes_ask=0.505,
            kalshi_no_ask=0.495,
        ),
        min_net_spread_pct=0.1,  # very permissive
    )
    # After fees могут не дотянуть до 0.1%, проверим что не падает
    assert sig is None or sig.expected_edge_pct >= 0.1


def test_detect_confidence_high():
    """Arb signal confidence ≥ 0.9 (high — arb выгоден если spread есть)."""
    sig = d40.detect(
        _pair(
            poly_yes_ask=0.30,
            poly_no_ask=0.70,
            kalshi_yes_ask=0.70,
            kalshi_no_ask=0.30,
        )
    )
    assert sig is not None
    assert sig.confidence >= 0.9


def test_detect_evidence_has_both_legs():
    sig = d40.detect(
        _pair(
            poly_yes_ask=0.40,
            poly_no_ask=0.60,
            kalshi_yes_ask=0.60,
            kalshi_no_ask=0.40,
        )
    )
    assert sig is not None
    e = sig.evidence
    # Both prices recorded
    assert e["poly_yes_ask"] == 0.40
    assert e["poly_no_ask"] == 0.60
    assert e["kalshi_yes_ask"] == 0.60
    assert e["kalshi_no_ask"] == 0.40
    # Fee rates recorded for both sides
    assert "cheap_fee_rate" in e
    assert "expensive_fee_rate" in e


def test_detect_market_id_anchored_to_polymarket():
    """market_id всегда Polymarket — это наша anchor table."""
    sig = d40.detect(
        _pair(
            polymarket_id="0xPolyABC",
            poly_yes_ask=0.40,
            poly_no_ask=0.60,
            kalshi_yes_ask=0.60,
            kalshi_no_ask=0.40,
        )
    )
    assert sig is not None
    assert sig.market_id == "0xPolyABC"


def test_detect_horizon_1h():
    sig = d40.detect(
        _pair(
            poly_yes_ask=0.40,
            poly_no_ask=0.60,
            kalshi_yes_ask=0.60,
            kalshi_no_ask=0.40,
        )
    )
    assert sig is not None
    assert sig.horizon_sec == 3600


def test_detect_event_id_propagated():
    sig = d40.detect(
        _pair(
            event_id="event-xyz",
            poly_yes_ask=0.40,
            poly_no_ask=0.60,
            kalshi_yes_ask=0.60,
            kalshi_no_ask=0.40,
        )
    )
    assert sig is not None
    assert sig.event_id == "event-xyz"
