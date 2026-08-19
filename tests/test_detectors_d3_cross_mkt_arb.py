"""Тесты D3 CrossMktArb детектора."""

from __future__ import annotations

from app.detectors import d3_cross_mkt_arb
from app.detectors.contract import Signal
from app.detectors.d3_cross_mkt_arb import EventMarket


def _mkts(prices):
    return [EventMarket(market_id=f"m{i}", yes_price=p) for i, p in enumerate(prices)]


def test_emits_signal_when_sum_above_trigger():
    sigs = d3_cross_mkt_arb.detect(
        event_id="evt-1",
        markets=_mkts([0.55, 0.40, 0.35]),  # sum 1.30
        neg_risk=True,
        neg_risk_other=False,
        n_yes_winners=1,
    )
    assert len(sigs) == 1
    sig = sigs[0]
    assert isinstance(sig, Signal)
    assert sig.detector == "D3_cross_mkt_arb"
    assert sig.side == "NO"
    assert sig.market_id == "m0"  # max yes_price
    assert sig.event_id == "evt-1"
    assert sig.evidence["yes_sum"] == 1.3
    assert 0.0 < sig.fair_price < 1.0


def test_target_is_max_yes_price():
    sigs = d3_cross_mkt_arb.detect(
        event_id="evt-2",
        markets=_mkts([0.30, 0.70, 0.25]),  # sum 1.25, max idx=1
        neg_risk=True,
        neg_risk_other=False,
        n_yes_winners=1,
    )
    assert len(sigs) == 1
    assert sigs[0].market_id == "m1"


def test_skip_when_sum_below_trigger():
    sigs = d3_cross_mkt_arb.detect(
        event_id="evt",
        markets=_mkts([0.50, 0.40, 0.20]),  # sum 1.10
        neg_risk=True,
        neg_risk_other=False,
        n_yes_winners=1,
    )
    assert sigs == []


def test_skip_when_not_neg_risk():
    sigs = d3_cross_mkt_arb.detect(
        event_id="evt",
        markets=_mkts([0.55, 0.40, 0.35]),
        neg_risk=False,
        neg_risk_other=False,
        n_yes_winners=1,
    )
    assert sigs == []


def test_skip_when_has_catch_all_market():
    sigs = d3_cross_mkt_arb.detect(
        event_id="evt",
        markets=_mkts([0.55, 0.40, 0.35]),
        neg_risk=True,
        neg_risk_other=True,
        n_yes_winners=1,
    )
    assert sigs == []


def test_skip_when_multiple_yes_winners():
    sigs = d3_cross_mkt_arb.detect(
        event_id="evt",
        markets=_mkts([0.55, 0.40, 0.35]),
        neg_risk=True,
        neg_risk_other=False,
        n_yes_winners=2,
    )
    assert sigs == []


def test_skip_single_market():
    sigs = d3_cross_mkt_arb.detect(
        event_id="evt",
        markets=_mkts([0.95]),
        neg_risk=True,
        neg_risk_other=False,
        n_yes_winners=1,
    )
    assert sigs == []


def test_skip_extreme_prices():
    sigs = d3_cross_mkt_arb.detect(
        event_id="evt",
        markets=_mkts([0.995, 0.30, 0.30]),
        neg_risk=True,
        neg_risk_other=False,
        n_yes_winners=1,
    )
    assert sigs == []


def test_confidence_grows_with_overhang():
    small = d3_cross_mkt_arb.detect(
        event_id="evt",
        markets=_mkts([0.55, 0.40, 0.30]),  # sum 1.25
        neg_risk=True,
        neg_risk_other=False,
        n_yes_winners=1,
    )[0]
    big = d3_cross_mkt_arb.detect(
        event_id="evt",
        markets=_mkts([0.70, 0.50, 0.40]),  # sum 1.60
        neg_risk=True,
        neg_risk_other=False,
        n_yes_winners=1,
    )[0]
    assert big.confidence > small.confidence


def test_signal_is_serializable():
    sigs = d3_cross_mkt_arb.detect(
        event_id="evt",
        markets=_mkts([0.55, 0.40, 0.35]),
        neg_risk=True,
        neg_risk_other=False,
        n_yes_winners=1,
    )
    assert len(sigs) == 1
    raw = sigs[0].to_json()
    restored = Signal.from_json(raw)
    assert restored.detector == sigs[0].detector
    assert restored.event_id == sigs[0].event_id
