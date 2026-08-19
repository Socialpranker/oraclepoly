"""virtual_execution: simulate_place + simulate_resolve_maker.

Phase 0 Stage 2: conservative model — strict price-through + AS penalty.
См. docs/superpowers/plans/2026-05-honest-baseline/02-conservative-queue.md.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import virtual_execution as ve

# ── Taker FOK ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_simulate_place_taker_uses_best_ask():
    book = {"ask": 0.42, "bid": 0.40, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        res = await ve.simulate_place(token_id="tok", mode="taker", limit_price=None, stake_usd=100.0)
    assert res.status == "virtual_filled"
    assert res.fill_price == 0.42
    assert res.simulation_model_version == "v2_conservative"


@pytest.mark.asyncio
async def test_taker_includes_as_penalty():
    """Taker fill: AS penalty = P_informed * (ask-bid)/2.
    P_informed=0.5, spread=0.02 → half_spread=0.01 → penalty per share = 0.005."""
    book = {"ask": 0.42, "bid": 0.40, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        res = await ve.simulate_place(token_id="tok", mode="taker", limit_price=None, stake_usd=100.0)
    assert res.as_penalty_per_share == pytest.approx(0.005, abs=1e-6)
    # effective fill = observed + penalty (для buy side)
    assert res.effective_fill_price == pytest.approx(0.425, abs=1e-6)


# ── Maker GTC: strict price-through ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_maker_bid_EQUAL_limit_does_NOT_fill():
    """v2 critical fix: best_bid == limit_price → НЕ fill (это touches, не through).

    v1 поведение: filled. v2: virtual_placed + записываем queue size."""
    book = {"ask": 0.42, "bid": 0.41, "ask_size": 100.0, "bid_size": 250.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        res = await ve.simulate_place(token_id="tok", mode="maker", limit_price=0.41, stake_usd=100.0)
    assert res.status == "virtual_placed"
    assert res.limit_price == 0.41
    # Queue size at top = 250 USD (bid_size); записывается при v2 placement
    assert res.queue_position_at_placement == 250.0
    assert res.simulation_model_version == "v2_conservative"


@pytest.mark.asyncio
async def test_maker_bid_STRICTLY_ABOVE_limit_fills():
    """v2: best_bid > limit → price moved through → fill at limit.
    AS penalty считается."""
    book = {"ask": 0.43, "bid": 0.42, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        res = await ve.simulate_place(token_id="tok", mode="maker", limit_price=0.41, stake_usd=100.0)
    assert res.status == "virtual_filled"
    assert res.fill_price == 0.41
    # half_spread = 0.005, penalty = 0.5*0.005 = 0.0025
    assert res.as_penalty_per_share == pytest.approx(0.0025, abs=1e-6)
    assert res.effective_fill_price == pytest.approx(0.4125, abs=1e-6)


@pytest.mark.asyncio
async def test_simulate_place_maker_waits_when_bid_below_limit():
    """Best_bid < limit → ждём в стакане. queue_position записан (= 0 если нет данных)."""
    book = {"ask": 0.42, "bid": 0.40, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        res = await ve.simulate_place(token_id="tok", mode="maker", limit_price=0.41, stake_usd=100.0)
    assert res.status == "virtual_placed"
    assert res.limit_price == 0.41


@pytest.mark.asyncio
async def test_simulate_place_no_book_returns_failed():
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=None)):
        res = await ve.simulate_place(token_id="tok", mode="taker", limit_price=None, stake_usd=100.0)
    assert res.status == "virtual_failed"
    assert res.reason == "no_book"


@pytest.mark.asyncio
async def test_simulate_place_maker_no_limit_failed():
    book = {"ask": 0.42, "bid": 0.40, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        res = await ve.simulate_place(token_id="tok", mode="maker", limit_price=None, stake_usd=100.0)
    assert res.status == "virtual_failed"
    assert res.reason == "invalid_limit"


@pytest.mark.asyncio
async def test_synthetic_fallback_when_no_book():
    """fallback_price → synthetic fill, no AS (нет book нет spread)."""
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=None)):
        res = await ve.simulate_place(
            token_id="tok", mode="taker", limit_price=None, fallback_price=0.30, stake_usd=100.0
        )
    assert res.status == "virtual_filled"
    assert res.fill_price == 0.30
    assert res.as_penalty_per_share == 0.0
    assert res.reason == "synthetic_no_book"


# ── Maker resolve: price-through OR queue cleared ───────────────────────────


@pytest.mark.asyncio
async def test_resolve_maker_price_moved_THROUGH_fills():
    """v2: best_bid > limit на resolve → fill (price moved through)."""
    book = {"ask": 0.44, "bid": 0.43, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        res = await ve.simulate_resolve_maker(
            token_id="tok",
            limit_price=0.41,
            age_sec=120.0,
            queue_position_at_placement=0.0,
            observed_traded_volume_at_level=0.0,
        )
    assert res.status == "virtual_filled"
    assert res.fill_price == 0.41
    assert res.reason == "price_through"
    # AS penalty: half_spread = 0.005, penalty = 0.0025
    assert res.as_penalty_per_share == pytest.approx(0.0025, abs=1e-6)


@pytest.mark.asyncio
async def test_resolve_maker_bid_EQUAL_limit_not_fill_until_queue_cleared():
    """v2 critical: bid == limit + queue_position > 0 → НЕ fill until traded_volume >= queue."""
    book = {"ask": 0.43, "bid": 0.41, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        # Queue 1000, traded 500 → не cleared, still open
        res = await ve.simulate_resolve_maker(
            token_id="tok",
            limit_price=0.41,
            age_sec=120.0,
            queue_position_at_placement=1000.0,
            observed_traded_volume_at_level=500.0,
        )
    assert res.status == "still_open"


@pytest.mark.asyncio
async def test_resolve_maker_queue_CLEARED_fills():
    """traded_volume >= queue → fill."""
    book = {"ask": 0.43, "bid": 0.41, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        res = await ve.simulate_resolve_maker(
            token_id="tok",
            limit_price=0.41,
            age_sec=120.0,
            queue_position_at_placement=1000.0,
            observed_traded_volume_at_level=1200.0,
        )
    assert res.status == "virtual_filled"
    assert res.reason == "queue_cleared"


@pytest.mark.asyncio
async def test_resolve_maker_conservative_default_observed_zero_no_fill():
    """v2 без queue tracking (observed=0 по default): даже bid==limit → still_open.

    Это conservative fallback пока не подключён trades feed. Объясняет почему D36
    fill rate упадёт с 100% — большинство fills на v1 были bid==limit edge cases."""
    book = {"ask": 0.43, "bid": 0.41, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        # Defaults observed_traded=0, queue=0 (placed when bid was below)
        res = await ve.simulate_resolve_maker(token_id="tok", limit_price=0.41, age_sec=120.0)
    assert res.status == "still_open"


@pytest.mark.asyncio
async def test_resolve_maker_stale_cancelled():
    """Bid ниже limit + age > 5 мин → stale_timeout."""
    book = {"ask": 0.43, "bid": 0.40, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        res = await ve.simulate_resolve_maker(token_id="tok", limit_price=0.41, age_sec=400.0)
    assert res.status == "virtual_cancelled"
    assert res.reason == "stale_timeout"


@pytest.mark.asyncio
async def test_resolve_maker_no_book_treats_as_open_until_stale():
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=None)):
        res = await ve.simulate_resolve_maker(token_id="tok", limit_price=0.41, age_sec=60.0)
    assert res.status == "still_open"


@pytest.mark.asyncio
async def test_resolve_maker_no_book_stale_cancelled():
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=None)):
        res = await ve.simulate_resolve_maker(token_id="tok", limit_price=0.41, age_sec=400.0)
    assert res.status == "virtual_cancelled"


# ── Model version marker ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_results_marked_v2_conservative():
    """Каждый VirtualPlaceResult / VirtualResolveResult имеет simulation_model_version='v2_conservative'."""
    book = {"ask": 0.42, "bid": 0.40, "ask_size": 100.0, "bid_size": 100.0}
    with patch("app.services.virtual_execution._fetch_book", new=AsyncMock(return_value=book)):
        r1 = await ve.simulate_place(token_id="t", mode="taker", limit_price=None, stake_usd=100.0)
        r2 = await ve.simulate_place(token_id="t", mode="maker", limit_price=0.50, stake_usd=100.0)
        r3 = await ve.simulate_resolve_maker(token_id="t", limit_price=0.50, age_sec=10.0)

    for r in (r1, r2, r3):
        assert r.simulation_model_version == "v2_conservative"


# ── AS penalty helper ────────────────────────────────────────────────────────


def test_as_penalty_with_no_book():
    """No book → no penalty (можно использовать synthetic fallback fill)."""
    assert ve._adverse_selection_penalty_per_share(None) == 0.0


def test_as_penalty_polymarket_typical_spread():
    """Spread 3¢ на price 0.50 → half_spread = 1.5¢, penalty = 0.5*1.5¢ = 0.0075."""
    book = {"bid": 0.485, "ask": 0.515}
    penalty = ve._adverse_selection_penalty_per_share(book)
    assert penalty == pytest.approx(0.0075, abs=1e-6)


def test_as_penalty_malformed_book():
    """bid >= ask → return 0 (нет valid spread)."""
    assert ve._adverse_selection_penalty_per_share({"bid": 0.50, "ask": 0.50}) == 0.0
    assert ve._adverse_selection_penalty_per_share({"bid": 0.60, "ask": 0.50}) == 0.0
    assert ve._adverse_selection_penalty_per_share({}) == 0.0
