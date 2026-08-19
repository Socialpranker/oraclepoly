"""Tests для D42 Event-driven ML detector."""

from __future__ import annotations

from app.detectors import d42_event_driven_ml


def _make_event(**overrides):
    # Дефолт — отрицательный delta (NO-плечо): после 2026-06-21 D42 эмитит ТОЛЬКО NO
    # (YES-плечо системно убыточно: WR 23%, sum_roi −1512 на live; edge только в NO).
    base = {
        "market_id": "0xM42",
        "event_id": "evt-42",
        "category": "Politics",
        "current_price": 0.50,
        "hours_to_resolve": 168.0,
        "predicted_delta_pp": -0.10,  # −10pp predicted → NO
        "window_name": "168_to_24",
        "clob_token_ids": None,
    }
    base.update(overrides)
    return base


class TestEmitConditions:
    def test_positive_delta_no_signal(self):
        # YES-плечо отрезано: positive delta (price will rise → ride up) больше не эмитится.
        assert d42_event_driven_ml.detect(_make_event(predicted_delta_pp=0.10)) is None

    def test_negative_delta_emits_no(self):
        sig = d42_event_driven_ml.detect(_make_event(predicted_delta_pp=-0.10))
        assert sig is not None
        assert sig.side == "NO"
        # entry = 1 - 0.50 = 0.50; fair = entry + |delta| = 0.60
        assert sig.current_price == 0.50
        assert sig.fair_price == 0.60


class TestRejectConditions:
    def test_small_delta_rejected(self):
        assert d42_event_driven_ml.detect(_make_event(predicted_delta_pp=0.03)) is None

    def test_zero_delta_rejected(self):
        assert d42_event_driven_ml.detect(_make_event(predicted_delta_pp=0.0)) is None

    def test_extreme_yes_zone_rejected(self):
        assert d42_event_driven_ml.detect(_make_event(current_price=0.10)) is None
        assert d42_event_driven_ml.detect(_make_event(current_price=0.90)) is None

    def test_missing_market_id_rejected(self):
        assert d42_event_driven_ml.detect(_make_event(market_id=None)) is None


class TestEvidenceFields:
    def test_evidence_contains_window_name(self):
        sig = d42_event_driven_ml.detect(_make_event(window_name="72_to_24"))
        assert sig is not None
        assert sig.evidence["window_name"] == "72_to_24"

    def test_evidence_contains_predicted_delta(self):
        sig = d42_event_driven_ml.detect(_make_event(predicted_delta_pp=-0.08))
        assert sig is not None
        assert sig.evidence["predicted_delta_pp"] == -0.08


class TestEdgeCalculation:
    def test_horizon_sec_is_24h(self):
        sig = d42_event_driven_ml.detect(_make_event())
        assert sig is not None
        assert sig.horizon_sec == 24 * 3600

    def test_confidence_matches_empirical_wr(self):
        sig = d42_event_driven_ml.detect(_make_event())
        assert sig is not None
        assert sig.confidence == d42_event_driven_ml.EMPIRICAL_WR
