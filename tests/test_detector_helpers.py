"""Parity tests для shared detector helpers.

Проверяют что новый параметризированный bucket_tx_hash() даёт ровно те же
hash что 6 legacy локальных функций до consolidation. Любое изменение
формулы → дубликаты signals в БД → падение этих тестов = катастрофа.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.detectors.contract import Signal
from app.services.detector_helpers import bucket_tx_hash, rank_signal

TS = datetime(2026, 5, 5, 14, 30, tzinfo=UTC)  # 14:00 → bucket aligned to 12:00 (4h) или 12:00 (6h)


def _legacy_d5(market_id: str, ts: datetime) -> str:
    bucket = ts.strftime("%Y%m%d%H")
    bucket_6h = f"{bucket[:8]}{int(bucket[8:]) // 6 * 6:02d}"
    raw = f"{market_id}|{bucket_6h}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:62]


def _legacy_d7b(market_id: str, ts: datetime) -> str:
    bucket = ts.strftime("%Y%m%d%H")
    bucket_4h = f"{bucket[:8]}{int(bucket[8:]) // 4 * 4:02d}"
    raw = f"{market_id}|{bucket_4h}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:62]


def _legacy_d10b(event_id: str, ts: datetime) -> str:
    # Same shape as D7b — отличается только тип ID.
    return _legacy_d7b(event_id, ts)


def _legacy_d13(market_id: str, ts: datetime) -> str:
    bucket = ts.strftime("%Y%m%d%H")
    bucket_4h = f"{bucket[:8]}{int(bucket[8:]) // 4 * 4:02d}"
    raw = f"{market_id}|d13|{bucket_4h}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:62]


def _legacy_d14(market_id: str, ts: datetime) -> str:
    bucket = ts.strftime("%Y%m%d%H")
    bucket_4h = f"{bucket[:8]}{int(bucket[8:]) // 4 * 4:02d}"
    raw = f"{market_id}|d14|{bucket_4h}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:62]


def _legacy_d_esports(market_id: str, ts: datetime) -> str:
    bucket = ts.strftime("%Y%m%d%H")
    bucket_4h = f"{bucket[:8]}{int(bucket[8:]) // 4 * 4:02d}"
    raw = f"{market_id}|d_esports_fav_fade|{bucket_4h}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:62]


def test_bucket_tx_hash_d5_parity():
    assert (
        bucket_tx_hash(bucket_id="0xmarket1", ts=TS, bucket_hours=6)
        == _legacy_d5("0xmarket1", TS)
    )


def test_bucket_tx_hash_d7b_parity():
    assert bucket_tx_hash(bucket_id="0xmarket1", ts=TS) == _legacy_d7b("0xmarket1", TS)


def test_bucket_tx_hash_d10b_parity():
    assert bucket_tx_hash(bucket_id="event_xyz", ts=TS) == _legacy_d10b("event_xyz", TS)


def test_bucket_tx_hash_d13_parity():
    assert (
        bucket_tx_hash(bucket_id="0xmarket1", ts=TS, namespace="d13")
        == _legacy_d13("0xmarket1", TS)
    )


def test_bucket_tx_hash_d14_parity():
    assert (
        bucket_tx_hash(bucket_id="0xmarket1", ts=TS, namespace="d14")
        == _legacy_d14("0xmarket1", TS)
    )


def test_bucket_tx_hash_d_esports_parity():
    assert (
        bucket_tx_hash(bucket_id="0xmarket1", ts=TS, namespace="d_esports_fav_fade")
        == _legacy_d_esports("0xmarket1", TS)
    )


def test_bucket_alignment_4h():
    """14:30 UTC → bucket 12 (12 + 4 = 16, 14 < 16)."""
    h = bucket_tx_hash(bucket_id="m", ts=datetime(2026, 5, 5, 14, 30, tzinfo=UTC))
    h_same = bucket_tx_hash(bucket_id="m", ts=datetime(2026, 5, 5, 15, 59, tzinfo=UTC))
    h_next = bucket_tx_hash(bucket_id="m", ts=datetime(2026, 5, 5, 16, 0, tzinfo=UTC))
    assert h == h_same
    assert h != h_next


def test_bucket_alignment_6h():
    """13:00 → bucket 12, 18:00 → bucket 18."""
    h = bucket_tx_hash(bucket_id="m", ts=datetime(2026, 5, 5, 13, 0, tzinfo=UTC), bucket_hours=6)
    h_same = bucket_tx_hash(bucket_id="m", ts=datetime(2026, 5, 5, 17, 59, tzinfo=UTC), bucket_hours=6)
    h_next = bucket_tx_hash(bucket_id="m", ts=datetime(2026, 5, 5, 18, 0, tzinfo=UTC), bucket_hours=6)
    assert h == h_same
    assert h != h_next


def _signal(*, confidence: float, current_price: float) -> Signal:
    return Signal(
        detector="X",
        market_id="m",
        side="NO",
        generated_at=TS,
        confidence=confidence,
        expected_edge_pct=0.0,
        fair_price=0.5,
        current_price=current_price,
        horizon_sec=3600,
    )


def test_rank_signal_higher_confidence_wins():
    a = _signal(confidence=0.10, current_price=0.5)
    b = _signal(confidence=0.20, current_price=0.5)
    assert rank_signal(b) > rank_signal(a)


def test_rank_signal_lower_price_wins():
    """1/price = payout multiplier — дешевле = выгоднее."""
    a = _signal(confidence=0.15, current_price=0.20)
    b = _signal(confidence=0.15, current_price=0.10)
    assert rank_signal(b) > rank_signal(a)


def test_rank_signal_zero_price_safe():
    a = _signal(confidence=0.5, current_price=0.0)
    assert rank_signal(a) == 0.0
