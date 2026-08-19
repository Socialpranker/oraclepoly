"""D45 LeadLagCrossVenue — unit tests (TDD).

PDF §7.1: венчер A двинулся первым → ставим на convergence венчера B.
DTE окно lookback 5-30 min, threshold 3% spread между leader и lagger.
Hold 30s-30min, time stop при не-convergence.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.detectors import d45_lead_lag_cross_venue as d45


def _pair(
    *,
    poly_yes_old: float = 0.45,
    poly_yes_now: float = 0.60,  # moved +15pp
    kalshi_yes_old: float = 0.45,
    kalshi_yes_now: float = 0.46,  # +1pp (lagging)
    lookback_sec: int = 600,  # 10 min
    inverse_side: bool = False,
    poly_volume_24h: float = 50_000.0,
    kalshi_volume_24h_usd: float = 30_000.0,
    poly_clob_token_ids: list | None = None,
):
    """Default: Polymarket moved +15pp за 10 min, Kalshi lagging by +1pp."""
    now = datetime.now(UTC)
    return d45.LeadLagPair(
        polymarket_id="poly_m1",
        kalshi_ticker="KALSHI-T1",
        event_id="evt-1",
        category="politics",
        inverse_side=inverse_side,
        poly_yes_old=poly_yes_old,
        poly_yes_now=poly_yes_now,
        kalshi_yes_old=kalshi_yes_old,
        kalshi_yes_now=kalshi_yes_now,
        poly_volume_24h=poly_volume_24h,
        kalshi_volume_24h_usd=kalshi_volume_24h_usd,
        lookback_sec=lookback_sec,
        snapshot_at=now,
        poly_clob_token_ids=poly_clob_token_ids or ["yes_token", "no_token"],
    )


def test_emit_kalshi_lags_poly_moved_up():
    """Polymarket двинулся +15pp, Kalshi почти статичен — bet kalshi convergence."""
    sig = d45.detect(_pair())
    assert sig is not None
    assert sig.detector == d45.DETECTOR_NAME
    assert sig.side == "YES"  # ставим на kalshi YES converge up
    assert sig.evidence["leader_venue"] == "polymarket"
    assert sig.evidence["lag_venue"] == "kalshi"
    assert sig.evidence["leader_delta_pp"] == pytest.approx(15.0, abs=0.1)
    assert sig.evidence["lag_delta_pp"] == pytest.approx(1.0, abs=0.1)
    assert sig.evidence["shadow_only"] is True


def test_emit_poly_lags_kalshi_moved_up():
    """Kalshi двинулся первым — bet polymarket YES converge up."""
    sig = d45.detect(_pair(
        poly_yes_old=0.45, poly_yes_now=0.46,
        kalshi_yes_old=0.45, kalshi_yes_now=0.60,
    ))
    assert sig is not None
    assert sig.evidence["leader_venue"] == "kalshi"
    assert sig.evidence["lag_venue"] == "polymarket"
    assert sig.side == "YES"
    # market_id = polymarket (lagging venue — where we trade)
    assert sig.market_id == "poly_m1"
    assert sig.token_id == "yes_token"


def test_emit_leader_moved_down_bet_no():
    """Leader двинулся вниз → bet NO on lagger (down convergence)."""
    sig = d45.detect(_pair(
        poly_yes_old=0.60, poly_yes_now=0.45,  # -15pp
        kalshi_yes_old=0.60, kalshi_yes_now=0.59,
    ))
    assert sig is not None
    assert sig.side == "NO"
    assert sig.token_id == "no_token"


def test_skip_both_moved_no_lag():
    """Оба двинулись одинаково — нет lead-lag edge."""
    sig = d45.detect(_pair(
        poly_yes_old=0.45, poly_yes_now=0.60,
        kalshi_yes_old=0.45, kalshi_yes_now=0.58,  # тоже почти +15pp
    ))
    assert sig is None


def test_skip_neither_moved():
    """Никто не двинулся — нет тригера."""
    sig = d45.detect(_pair(
        poly_yes_old=0.50, poly_yes_now=0.505,
        kalshi_yes_old=0.50, kalshi_yes_now=0.501,
    ))
    assert sig is None


def test_skip_inverse_pair():
    """Inverse semantics (Phase 1 skip per D40 precedent)."""
    sig = d45.detect(_pair(inverse_side=True))
    assert sig is None


def test_skip_low_volume_poly():
    sig = d45.detect(_pair(poly_volume_24h=500.0))
    assert sig is None


def test_skip_low_volume_kalshi():
    sig = d45.detect(_pair(kalshi_volume_24h_usd=200.0))
    assert sig is None


def test_skip_lookback_too_short():
    """Минимум 5 min lookback (snapshot noise защита)."""
    sig = d45.detect(_pair(lookback_sec=60))  # 1 min — too short
    assert sig is None


def test_skip_lookback_too_long():
    """Максимум 30 min (PDF: hold 30 sec - 30 min — lag должен быть свежим)."""
    sig = d45.detect(_pair(lookback_sec=3600))  # 1 hour
    assert sig is None


def test_skip_below_threshold():
    """Leader двинулся только 1.5pp — ниже threshold 3pp."""
    sig = d45.detect(_pair(
        poly_yes_old=0.50, poly_yes_now=0.515,
        kalshi_yes_old=0.50, kalshi_yes_now=0.501,
    ))
    assert sig is None


def test_skip_missing_fields():
    """Любой None price → skip."""
    pair = _pair()
    bad = d45.LeadLagPair(
        polymarket_id=pair.polymarket_id,
        kalshi_ticker=pair.kalshi_ticker,
        event_id=pair.event_id,
        category=pair.category,
        inverse_side=pair.inverse_side,
        poly_yes_old=None,
        poly_yes_now=pair.poly_yes_now,
        kalshi_yes_old=pair.kalshi_yes_old,
        kalshi_yes_now=pair.kalshi_yes_now,
        poly_volume_24h=pair.poly_volume_24h,
        kalshi_volume_24h_usd=pair.kalshi_volume_24h_usd,
        lookback_sec=pair.lookback_sec,
        snapshot_at=pair.snapshot_at,
        poly_clob_token_ids=pair.poly_clob_token_ids,
    )
    assert d45.detect(bad) is None


def test_edge_and_fair_price_set():
    """Signal должен иметь sensible edge + fair_price + horizon_sec."""
    sig = d45.detect(_pair())
    assert sig is not None
    assert sig.fair_price > 0 and sig.fair_price < 1
    assert sig.expected_edge_pct > 0
    # horizon = 30 min default
    assert sig.horizon_sec == d45.HORIZON_SEC
