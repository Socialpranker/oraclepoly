"""D44 LateConvergenceCarry — unit tests (TDD).

Buy near $0.96-0.98 (YES) или $0.02-0.04 (NO), hold to resolve.
DTE window 2h-7d (exclude D38 territory near end_date).
Category whitelist + keyword blacklist (skip geopolitics/subjective).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.detectors import d44_late_convergence_carry as d44


def _event(**overrides):
    """Default: crypto market в 24h, YES = 0.97, ликвидно."""
    base = {
        "market_id": "0xmkt1",
        "condition_id": "0xcond1",
        "category": "crypto",
        "question": "Will BTC close above $100k on Friday?",
        "current_price": 0.97,
        "end_date": datetime.now(UTC) + timedelta(hours=24),
        "volume_24h": 50_000.0,
        "liquidity": 10_000.0,
        "clob_token_ids": ["yes_token", "no_token"],
        "event_id": "evt-1",
    }
    base.update(overrides)
    return base


def test_emit_yes_signal():
    sig = d44.detect(_event())
    assert sig is not None
    assert sig.side == "YES"
    assert sig.detector == d44.DETECTOR_NAME
    assert sig.confidence == pytest.approx(0.96)
    assert sig.token_id == "yes_token"
    assert sig.evidence["category"] == "crypto"
    assert sig.evidence["shadow_only"] is True
    assert sig.evidence["dte_hours"] == pytest.approx(24.0, abs=0.1)


def test_skip_geopolitics_keyword():
    """Question matching blacklist → skip даже если category whitelisted."""
    ev = _event(question="Will Ukraine sign mineral deal by Friday?")
    assert d44.detect(ev) is None


def test_skip_subjective_keyword():
    """'wear' — subjective resolution criteria."""
    ev = _event(question="Will Zelensky wear a suit before July?")
    assert d44.detect(ev) is None


def test_skip_low_volume():
    """volume_24h < $10K → skip."""
    assert d44.detect(_event(volume_24h=5_000.0)) is None
    assert d44.detect(_event(volume_24h=0.0)) is None


def test_skip_low_liquidity():
    """liquidity < $5K → skip."""
    assert d44.detect(_event(liquidity=1_000.0)) is None


def test_skip_past_end_date():
    """End_date в прошлом → D38 territory, не наша."""
    assert d44.detect(_event(end_date=datetime.now(UTC) - timedelta(hours=2))) is None


def test_skip_within_2h():
    """DTE < 2h → слишком близко к end_date (D38 overlap)."""
    assert d44.detect(_event(end_date=datetime.now(UTC) + timedelta(hours=1))) is None


def test_skip_beyond_7d():
    """DTE > 7d → carry слишком долгий."""
    assert d44.detect(_event(end_date=datetime.now(UTC) + timedelta(days=10))) is None


def test_skip_price_out_of_band():
    """0.85 не попадает ни в YES band (0.96-0.98), ни в NO band (0.02-0.04)."""
    assert d44.detect(_event(current_price=0.85)) is None
    assert d44.detect(_event(current_price=0.50)) is None
    assert d44.detect(_event(current_price=0.15)) is None


def test_emit_no_signal_low_band():
    """current_price = 0.03 → NO side (entry = 1 - 0.03 = 0.97)."""
    sig = d44.detect(_event(current_price=0.03))
    assert sig is not None
    assert sig.side == "NO"
    assert sig.token_id == "no_token"
    # current_price канонично = YES price
    assert sig.current_price == pytest.approx(0.03)


def test_skip_non_whitelist_category():
    """politics not in whitelist → skip."""
    assert d44.detect(_event(category="politics")) is None
    assert d44.detect(_event(category="culture")) is None


def test_skip_sports_category():
    """sports убран из whitelist: 66% win, но −34% net на 18 сигналах (autoresearch 2026-05)."""
    assert d44.detect(_event(category="sports")) is None


def test_skip_missing_market_id():
    assert d44.detect(_event(market_id=None)) is None


def test_skip_missing_end_date():
    assert d44.detect(_event(end_date=None)) is None
