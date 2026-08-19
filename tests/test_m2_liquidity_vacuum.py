"""Tests for M2 liquidity vacuum measurement detector."""
from __future__ import annotations

from app.detectors.m2_liquidity_vacuum import (
    HORIZON_SEC,
    detect,
)


def _make_event(
    *,
    yes_midpoint: float = 0.50,
    depth_bid: float = 1000.0,
    depth_ask: float = 100.0,
    depleted_side: str = "ask",
    whale_in_window: bool = False,
) -> dict:
    """Синтетический market_event как его собирает _m2_emit_async."""
    hi, lo = max(depth_bid, depth_ask), min(depth_bid, depth_ask)
    ratio = lo / hi if hi > 0 else 1.0
    return {
        "market_id": "0xabc",
        "event_id": "event_123",
        "token_id": "yes_tok",
        "category": "Sports",
        "yes_midpoint": yes_midpoint,
        "depth_bid": depth_bid,
        "depth_ask": depth_ask,
        "depleted_side": depleted_side,
        "depletion_ratio": ratio,
        "whale_in_window": whale_in_window,
        "detected_at": "2026-06-21T12:00:00+00:00",
    }


def test_happy_path_emits_signal():
    sig = detect(_make_event())
    assert sig is not None
    assert sig.detector == "M2_liquidity_vacuum"
    assert sig.horizon_sec == HORIZON_SEC


# ── 2.1 Depletion ratio ──


def test_depletion_ratio_below_threshold_emits():
    # depth_ask=100, depth_bid=1000 → ratio 0.1 < 0.30 → сигнал
    sig = detect(_make_event(depth_bid=1000.0, depth_ask=100.0, depleted_side="ask"))
    assert sig is not None
    assert sig.evidence["depletion_ratio"] == 0.1


def test_depletion_ratio_at_or_above_threshold_none():
    # depth_ask=400, depth_bid=1000 → ratio 0.4 >= 0.30 → None
    assert detect(_make_event(depth_bid=1000.0, depth_ask=400.0, depleted_side="ask")) is None


def test_depletion_ratio_exactly_threshold_none():
    # ratio == 0.30 → None (строгое <)
    assert detect(_make_event(depth_bid=1000.0, depth_ask=300.0, depleted_side="ask")) is None


def test_zero_max_depth_none():
    assert detect(_make_event(depth_bid=0.0, depth_ask=0.0)) is None


# ── 2.2 Side-логика (ask→YES, bid→NO) ──


def test_side_asks_depleted_is_yes():
    # asks вымели (depth_ask << depth_bid) → buy-pressure → YES
    sig = detect(_make_event(depth_bid=1000.0, depth_ask=100.0, depleted_side="ask"))
    assert sig is not None
    assert sig.side == "YES"


def test_side_bids_depleted_is_no():
    # bids вымели (depth_bid << depth_ask) → sell-pressure → YES↓ → NO
    sig = detect(_make_event(depth_bid=100.0, depth_ask=1000.0, depleted_side="bid"))
    assert sig is not None
    assert sig.side == "NO"


# ── 2.3 Mid-зона гейт ──


def test_mid_zone_too_low_none():
    assert detect(_make_event(yes_midpoint=0.05)) is None


def test_mid_zone_too_high_none():
    assert detect(_make_event(yes_midpoint=0.95)) is None


def test_mid_zone_center_emits():
    assert detect(_make_event(yes_midpoint=0.50)) is not None


def test_mid_zone_missing_none():
    ev = _make_event()
    ev["yes_midpoint"] = None
    assert detect(ev) is None


# ── 2.4 Floor гейт (с временным override константы) ──


def test_floor_below_threshold_none(monkeypatch):
    import app.detectors.m2_liquidity_vacuum as m2

    monkeypatch.setattr(m2, "MIN_TOTAL_DEPTH_SHARES", 5000.0)
    # depth_bid+depth_ask = 1100 < 5000 → None
    assert m2.detect(_make_event(depth_bid=1000.0, depth_ask=100.0)) is None


def test_floor_zero_default_does_not_filter():
    # дефолт 0.0 → тонкий стакан проходит floor (depletion решает)
    sig = detect(_make_event(depth_bid=10.0, depth_ask=1.0, depleted_side="ask"))
    assert sig is not None


# ── 2.5 Контракт current_price + evidence ──


def test_current_price_equals_yes_midpoint():
    sig = detect(_make_event(yes_midpoint=0.42))
    assert sig is not None
    assert sig.current_price == 0.42
    assert sig.fair_price == 0.42  # = current → divergence 0
    assert sig.evidence["mid_price"] == 0.42
    assert sig.divergence == 0.0


def test_evidence_completeness():
    sig = detect(_make_event())
    assert sig is not None
    for key in (
        "depth_bid", "depth_ask", "depletion_ratio", "depleted_side",
        "mid_price", "whale_in_window", "detected_at", "category",
        "tx_hash", "strategy",
    ):
        assert key in sig.evidence, f"missing evidence key: {key}"
    assert sig.evidence["strategy"] == "liquidity_vacuum"


def test_whale_in_window_propagated():
    sig = detect(_make_event(whale_in_window=True))
    assert sig is not None
    assert sig.evidence["whale_in_window"] is True


# ── 2.6 Заглушки confidence/edge + horizon ──


def test_confidence_and_edge_are_stubs():
    sig = detect(_make_event())
    assert sig is not None
    assert sig.confidence == 0.50
    assert sig.expected_edge_pct == 0.0


def test_horizon_is_one_hour():
    sig = detect(_make_event())
    assert sig is not None
    assert sig.horizon_sec == 3600


# ── Regression: markets.event_id UUID-колонка → asyncpg insert падал на $3 (prod 2026-06-21) ──


def test_event_id_uuid_coerced_to_str():
    """markets.event_id приходит как uuid.UUID (asyncpg) → Signal.event_id должен быть str."""
    import uuid

    ev = _make_event()
    ev["event_id"] = uuid.UUID("b4b84caf-44ea-429a-96cf-fd89a6072600")
    sig = detect(ev)
    assert sig is not None
    assert sig.event_id == "b4b84caf-44ea-429a-96cf-fd89a6072600"
    assert isinstance(sig.event_id, str)


def test_event_id_none_preserved():
    """event_id отсутствует/None → остаётся None (не строка 'None')."""
    ev = _make_event()
    ev["event_id"] = None
    sig = detect(ev)
    assert sig is not None
    assert sig.event_id is None
