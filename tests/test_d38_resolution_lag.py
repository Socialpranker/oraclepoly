"""D38 ResolutionLag — unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.detectors import d38_resolution_lag as d38


def _event(**overrides):
    """Default: market прошёл end_date 3 часа назад, YES near-fait price 0.92."""
    base = {
        "market_id": "0xmarket1",
        "category": "Sports",
        "current_price": 0.92,
        "volume_24h": 5000.0,
        "end_date": datetime.now(UTC) - timedelta(hours=3),
        "clob_token_ids": ["yes_token", "no_token"],
    }
    base.update(overrides)
    return base


def test_detect_yes_near_fait_emits():
    sig = d38.detect(_event())
    assert sig is not None
    assert sig.side == "YES"
    assert sig.detector == "D38_resolution_lag"
    assert sig.current_price == pytest.approx(0.92)
    assert sig.confidence == pytest.approx(0.95)
    assert sig.evidence["zone"] == "yes_near_fait"
    assert sig.evidence["lag_hours"] == pytest.approx(3.0, abs=0.05)
    assert sig.token_id == "yes_token"


def test_detect_no_near_fait_emits():
    sig = d38.detect(_event(current_price=0.10))
    assert sig is not None
    assert sig.side == "NO"
    assert sig.evidence["zone"] == "no_near_fait"
    # current_price = YES canonical = 0.10 (NO entry cost = 1 - 0.10 = 0.90)
    assert sig.current_price == pytest.approx(0.10)
    assert sig.token_id == "no_token"


def test_detect_skip_future_end_date():
    """end_date в будущем — рынок ещё не закончился."""
    assert d38.detect(_event(end_date=datetime.now(UTC) + timedelta(hours=1))) is None


def test_detect_skip_stuck_market():
    """lag > 24h — stuck/dispute, не торгуем."""
    assert d38.detect(_event(end_date=datetime.now(UTC) - timedelta(hours=30))) is None
    assert d38.detect(_event(end_date=datetime.now(UTC) - timedelta(days=10))) is None


def test_detect_skip_uncertain_zone():
    """Зона неопределённости 0.20-0.80 — рынок не зацементирован."""
    assert d38.detect(_event(current_price=0.50)) is None
    assert d38.detect(_event(current_price=0.65)) is None
    assert d38.detect(_event(current_price=0.30)) is None


def test_detect_skip_already_resolved_price():
    """Edge мизерный когда price уже на crawl близко к 0/1."""
    assert d38.detect(_event(current_price=0.96)) is None
    assert d38.detect(_event(current_price=0.99)) is None
    assert d38.detect(_event(current_price=0.04)) is None


def test_detect_skip_low_volume():
    assert d38.detect(_event(volume_24h=100.0)) is None
    assert d38.detect(_event(volume_24h=0.0)) is None
    assert d38.detect(_event(volume_24h=None)) is None


def test_detect_yes_zone_boundaries():
    """YES zone [0.80, 0.95)."""
    assert d38.detect(_event(current_price=0.80)) is not None
    assert d38.detect(_event(current_price=0.94)) is not None
    assert d38.detect(_event(current_price=0.95)) is None
    assert d38.detect(_event(current_price=0.79)) is None


def test_detect_no_zone_boundaries():
    """NO zone [0.05, 0.15) после tightening 2026-05-19.

    Tighten мотивирован эмпирикой: yes_price 0.15-0.20 bucket был WR 78%
    vs breakeven 82% → EV -$5.5/trade. См. NO_PRICE_CEILING docstring.
    """
    # Edge на 0.05: NO entry = 0.95, payout = 1.053, edge = 0.95*1.053-1 ≈ 0 → skip
    assert d38.detect(_event(current_price=0.05)) is None
    # А вот 0.06 уже даёт edge: NO entry = 0.94, edge = 0.95/0.94-1 = +1.06%
    assert d38.detect(_event(current_price=0.06)) is not None
    # Внутри новой зоны
    assert d38.detect(_event(current_price=0.10)) is not None
    assert d38.detect(_event(current_price=0.14)) is not None
    # Новая верхняя граница (exclusive)
    assert d38.detect(_event(current_price=0.15)) is None
    # Старая верхняя граница (была [0.05, 0.20)) — теперь skip
    assert d38.detect(_event(current_price=0.17)) is None
    assert d38.detect(_event(current_price=0.19)) is None
    # Прежний edge тест: 0.20 skip остаётся
    assert d38.detect(_event(current_price=0.20)) is None
    # Под нижней границей
    assert d38.detect(_event(current_price=0.04)) is None


def test_no_ceiling_constant_is_0_15():
    """Guard against accidental re-widening to 0.20 (см. tightening 2026-05-19)."""
    assert d38.NO_PRICE_CEILING == pytest.approx(0.15)


def test_detect_losing_bucket_yes_0_15_to_0_20_is_blocked():
    """Регрессия: убыточный bucket NO yes_price ∈ [0.15, 0.20) НЕ эмитит.

    Эмпирика на 30d (n=69, WR 78.3%, EV -$5.5/trade) показала что в этой зоне
    breakeven WR (82%) выше observed → отрезаем целиком."""
    for yes_price in [0.15, 0.16, 0.17, 0.18, 0.19, 0.199]:
        assert d38.detect(_event(current_price=yes_price)) is None, (
            f"expected None for yes_price={yes_price} (post-2026-05-19 tightening)"
        )


def test_detect_lag_at_boundary():
    """Lag ровно 24h — допускается; >24h — отсекается."""
    just_under = d38.detect(_event(end_date=datetime.now(UTC) - timedelta(hours=23, minutes=50)))
    assert just_under is not None
    just_over = d38.detect(_event(end_date=datetime.now(UTC) - timedelta(hours=24, minutes=10)))
    assert just_over is None


def test_detect_horizon_6h():
    sig = d38.detect(_event())
    assert sig is not None
    assert sig.horizon_sec == 6 * 3600


def test_detect_invalid_inputs():
    assert d38.detect({"market_id": None}) is None
    assert d38.detect(_event(market_id=None)) is None
    assert d38.detect(_event(end_date=None)) is None
    assert d38.detect(_event(current_price="invalid")) is None
    assert d38.detect(_event(volume_24h="invalid")) is None


def test_detect_evidence_contains_required_fields():
    sig = d38.detect(_event())
    assert sig is not None
    evidence = sig.evidence
    assert "category" in evidence
    assert "zone" in evidence
    assert "lag_hours" in evidence
    assert "volume_24h" in evidence
    assert "raw_yes_price" in evidence
    assert "end_date" in evidence
    assert evidence["raw_yes_price"] == pytest.approx(0.92)


def test_detect_fair_price_below_entry():
    """fair_price < current_price (мы думаем что entry overpriced)."""
    sig = d38.detect(_event())
    assert sig is not None
    assert sig.fair_price < sig.current_price


def test_detect_token_id_resolves_per_side():
    yes_sig = d38.detect(_event(current_price=0.90))
    no_sig = d38.detect(_event(current_price=0.10))
    assert yes_sig is not None and no_sig is not None
    assert yes_sig.token_id == "yes_token"
    assert no_sig.token_id == "no_token"


def test_detect_works_without_token_ids():
    """Detector emits even если clob_token_ids None — token_id будет None."""
    sig = d38.detect(_event(clob_token_ids=None))
    assert sig is not None
    assert sig.token_id is None
