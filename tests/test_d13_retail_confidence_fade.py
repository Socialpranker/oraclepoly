"""D13 RetailConfidenceFade — unit tests."""

from __future__ import annotations

import pytest

from app.detectors import d13_retail_confidence_fade as d13


def _event(**overrides):
    base = {
        "market_id": "0xmarket1",
        "category": "Politics",
        "entry_price": 0.92,
        "hours_to_resolve": 24.0,
        "smart_money_taker_share": 0.02,  # retail-only
        "n_takers_24h_before": 10,
    }
    base.update(overrides)
    return base


def test_emits_for_retail_only_24h():
    sig = d13.detect(_event())
    assert sig is not None
    assert sig.side == "NO"
    assert sig.detector == "D13_retail_confidence_fade"
    # current_price = YES canonical = 0.92 (NO entry cost = 1 - 0.92 = 0.08)
    assert sig.current_price == pytest.approx(0.92, abs=0.001)
    # 24h baseline WR = 0.158
    assert sig.confidence == pytest.approx(0.158, abs=0.001)
    assert sig.evidence["anchor_band"] == "24h"
    assert sig.evidence["smart_money_taker_share"] == 0.02


def test_emits_for_4h_band():
    sig = d13.detect(_event(hours_to_resolve=5.0))
    assert sig is not None
    assert sig.evidence["anchor_band"] == "4h"
    assert sig.confidence == pytest.approx(0.163, abs=0.001)


def test_emits_for_72h_band():
    sig = d13.detect(_event(hours_to_resolve=60.0))
    assert sig is not None
    assert sig.evidence["anchor_band"] == "72h"
    assert sig.confidence == pytest.approx(0.164, abs=0.001)


def test_skip_smart_money_above_threshold():
    """smart_share >= 0.05 — есть smart money, не emit."""
    assert d13.detect(_event(smart_money_taker_share=0.05)) is None
    assert d13.detect(_event(smart_money_taker_share=0.10)) is None
    assert d13.detect(_event(smart_money_taker_share=0.5)) is None


def test_skip_smart_money_at_exact_threshold():
    """smart_share == 0.05 (>=) → skip."""
    assert d13.detect(_event(smart_money_taker_share=0.05)) is None


def test_emits_at_just_below_threshold():
    """smart_share = 0.049 — ниже cap, emit."""
    sig = d13.detect(_event(smart_money_taker_share=0.049))
    assert sig is not None


def test_skip_too_few_takers():
    """n_takers < 3 — недостаточно стат. значимости."""
    assert d13.detect(_event(n_takers_24h_before=2)) is None
    assert d13.detect(_event(n_takers_24h_before=0)) is None


def test_skip_zone_outside():
    """entry_price вне [0.85, 0.95)."""
    assert d13.detect(_event(entry_price=0.84)) is None
    assert d13.detect(_event(entry_price=0.95)) is None  # ceiling exclusive
    assert d13.detect(_event(entry_price=0.97)) is None


def test_emits_at_zone_boundaries():
    """entry_price = 0.85 (exact floor) emits."""
    sig = d13.detect(_event(entry_price=0.85))
    assert sig is not None


def test_skip_horizon_outside():
    assert d13.detect(_event(hours_to_resolve=3.0)) is None
    assert d13.detect(_event(hours_to_resolve=80.0)) is None


def test_skip_culture_category():
    assert d13.detect(_event(category="Culture")) is None


def test_skip_missing_smart_share():
    """smart_money_taker_share отсутствует → skip (D13 strict)."""
    e = _event()
    e.pop("smart_money_taker_share")
    assert d13.detect(e) is None


def test_skip_missing_n_takers():
    """n_takers отсутствует → skip."""
    e = _event()
    e.pop("n_takers_24h_before")
    assert d13.detect(e) is None


def test_skip_no_market_id():
    assert d13.detect(_event(market_id=None)) is None
    assert d13.detect(_event(market_id="")) is None


def test_evidence_includes_inputs():
    sig = d13.detect(_event(category="Crypto"))
    assert sig is not None
    assert sig.evidence["category"] == "Crypto"
    assert sig.evidence["yes_price"] == 0.92
    assert sig.evidence["smart_money_taker_share"] == 0.02
    assert sig.evidence["n_takers_24h_before"] == 10


def test_invalid_smart_share_type():
    """Если smart_share не parseable — skip."""
    assert d13.detect(_event(smart_money_taker_share="invalid")) is None
