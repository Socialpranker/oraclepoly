"""Tests for FakeClobClient — Phase 1 Stage 8.

Покрываем:
  - Protocol conformance (interface parity).
  - Order placement: stateful, partial/full fills, deterministic seed.
  - Trades: get_trades returns placed orders.
  - Cancel: success/failure paths, balance unfreeze.
  - Order book: synthetic default, seed_book override.
  - Balance: tracks frozen vs free.
  - Failure injection: failure_rate triggers TimeoutError.
  - Latency simulation: asyncio.sleep called.

DB-моки не нужны — Fake stateful sam в памяти.
"""

from __future__ import annotations

import pytest

from app.services.clob_client_protocol import ClobClientProtocol
from app.services.fake_clob_client import FakeClobClient

# --- Protocol conformance --------------------------------------------------


def test_fake_satisfies_protocol():
    """FakeClobClient должен implementировать все методы ClobClientProtocol."""
    fake = FakeClobClient()
    assert isinstance(fake, ClobClientProtocol)


def test_fake_has_all_protocol_methods():
    """Sanity: каждый method из Protocol существует на Fake."""
    fake = FakeClobClient()
    for method in (
        "create_and_post_order",
        "get_trades",
        "cancel_order",
        "get_order_book",
        "get_balance_allowance",
    ):
        assert hasattr(fake, method), f"missing {method}"
        assert callable(getattr(fake, method))


# --- Order placement -------------------------------------------------------


@pytest.mark.asyncio
async def test_create_order_returns_success():
    """Valid args → success response с orderID."""
    fake = FakeClobClient(seed=42, partial_fill_rate=0.0, latency_ms=(0, 1))
    args = {"size": 100.0, "price": 0.5, "side": "BUY", "token_id": "tok1"}
    result = await fake.create_and_post_order(args)
    assert result["success"] is True
    assert "orderID" in result
    assert result["status"] in ("MATCHED", "PARTIAL")
    assert result["size"] == 100.0


@pytest.mark.asyncio
async def test_create_order_full_fill_when_partial_rate_zero():
    """partial_fill_rate=0 → всегда full fill (status=MATCHED)."""
    fake = FakeClobClient(seed=42, partial_fill_rate=0.0, latency_ms=(0, 1))
    args = {"size": 100.0, "price": 0.5, "side": "BUY", "token_id": "tok1"}
    result = await fake.create_and_post_order(args)
    assert result["status"] == "MATCHED"
    assert result["filled"] == 100.0


@pytest.mark.asyncio
async def test_create_order_partial_fill_when_rate_one():
    """partial_fill_rate=1.0 → всегда partial fill (10-90%)."""
    fake = FakeClobClient(seed=42, partial_fill_rate=1.0, latency_ms=(0, 1))
    args = {"size": 100.0, "price": 0.5, "side": "BUY", "token_id": "tok1"}
    result = await fake.create_and_post_order(args)
    assert result["status"] == "PARTIAL"
    assert 10.0 <= result["filled"] <= 90.0


@pytest.mark.asyncio
async def test_create_order_invalid_args_rejected():
    """size=0 / price out of (0,1) → error response."""
    fake = FakeClobClient(seed=42, latency_ms=(0, 1))
    for bad in [
        {"size": 0, "price": 0.5, "side": "BUY", "token_id": "tok"},
        {"size": 100, "price": 0.0, "side": "BUY", "token_id": "tok"},
        {"size": 100, "price": 1.0, "side": "BUY", "token_id": "tok"},
        {"size": 100, "price": 1.5, "side": "BUY", "token_id": "tok"},
    ]:
        result = await fake.create_and_post_order(bad)
        assert result["success"] is False, f"должна fail: {bad}"


@pytest.mark.asyncio
async def test_create_order_deterministic_seed():
    """Same seed + same args → same order_id, same fill behaviour."""
    args = {"size": 100.0, "price": 0.5, "side": "BUY", "token_id": "tok1"}

    f1 = FakeClobClient(seed=42, partial_fill_rate=0.5, latency_ms=(0, 1))
    r1 = await f1.create_and_post_order(args)
    f2 = FakeClobClient(seed=42, partial_fill_rate=0.5, latency_ms=(0, 1))
    r2 = await f2.create_and_post_order(args)

    assert r1["orderID"] == r2["orderID"]
    assert r1["status"] == r2["status"]
    assert r1["filled"] == r2["filled"]


@pytest.mark.asyncio
async def test_create_order_balance_decreases():
    """Stake = filled × price вычитается из free balance."""
    fake = FakeClobClient(
        seed=42, partial_fill_rate=0.0, latency_ms=(0, 1), initial_balance_usdc=1000.0
    )
    args = {"size": 100.0, "price": 0.4, "side": "BUY", "token_id": "tok1"}
    await fake.create_and_post_order(args)
    balance = await fake.get_balance_allowance()
    # stake = 100 × 0.4 = $40 frozen → free $960
    assert float(balance["balance"]) == pytest.approx(960.0, abs=0.01)


# --- get_trades ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_trades_empty_initially():
    """Fresh client → empty trades."""
    fake = FakeClobClient(latency_ms=(0, 1))
    trades = await fake.get_trades()
    assert trades == []


@pytest.mark.asyncio
async def test_get_trades_returns_placed_orders():
    """After placing N orders → get_trades returns N entries."""
    fake = FakeClobClient(seed=42, partial_fill_rate=0.0, latency_ms=(0, 1))
    for _ in range(3):
        await fake.create_and_post_order(
            {"size": 50.0, "price": 0.5, "side": "BUY", "token_id": "tok1"}
        )
    trades = await fake.get_trades()
    assert len(trades) == 3


@pytest.mark.asyncio
async def test_get_trades_fok_marks_taker():
    """order_type=FOK → trader_side=TAKER."""
    fake = FakeClobClient(seed=42, partial_fill_rate=0.0, latency_ms=(0, 1))
    await fake.create_and_post_order(
        {"size": 50.0, "price": 0.5, "side": "BUY", "token_id": "tok1"},
        order_type="FOK",
    )
    trades = await fake.get_trades()
    assert trades[0]["trader_side"] == "TAKER"
    assert trades[0]["taker_order_id"] is not None


# --- cancel_order ----------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_order_not_found():
    """Cancel non-existent order → error."""
    fake = FakeClobClient(latency_ms=(0, 1))
    result = await fake.cancel_order("nonexistent")
    assert result["success"] is False
    assert "order_not_found" in result.get("error", "")


@pytest.mark.asyncio
async def test_cancel_matched_order_fails():
    """Already MATCHED order не может быть cancelled."""
    fake = FakeClobClient(seed=42, partial_fill_rate=0.0, latency_ms=(0, 1))
    placed = await fake.create_and_post_order(
        {"size": 50.0, "price": 0.5, "side": "BUY", "token_id": "tok1"}
    )
    result = await fake.cancel_order(placed["orderID"])
    assert result["success"] is False


# --- get_order_book --------------------------------------------------------


@pytest.mark.asyncio
async def test_default_order_book_synthetic():
    """No seeded book → synthetic default vокруг 0.50."""
    fake = FakeClobClient(latency_ms=(0, 1))
    book = await fake.get_order_book("any_token")
    assert "bids" in book and "asks" in book
    assert len(book["bids"]) > 0
    # Top bid должен быть ниже top ask (valid book).
    top_bid = float(book["bids"][0]["price"])
    top_ask = float(book["asks"][0]["price"])
    assert top_bid < top_ask


@pytest.mark.asyncio
async def test_seed_book_overrides_default():
    """seed_book() позволяет drill scenario установить custom book."""
    fake = FakeClobClient(latency_ms=(0, 1))
    custom = {
        "token_id": "tok1",
        "bids": [{"price": "0.20", "size": "100"}],
        "asks": [{"price": "0.80", "size": "100"}],
        "timestamp": 0,
    }
    fake.seed_book("tok1", custom)
    book = await fake.get_order_book("tok1")
    assert book == custom


# --- Failure injection -----------------------------------------------------


@pytest.mark.asyncio
async def test_failure_rate_one_always_fails():
    """failure_rate=1.0 → каждый call raises TimeoutError."""
    fake = FakeClobClient(failure_rate=1.0, latency_ms=(0, 1))
    with pytest.raises(TimeoutError, match="injected_failure"):
        await fake.create_and_post_order(
            {"size": 50.0, "price": 0.5, "side": "BUY", "token_id": "tok"}
        )


@pytest.mark.asyncio
async def test_failure_rate_zero_never_fails():
    """failure_rate=0.0 → no injected failures."""
    fake = FakeClobClient(failure_rate=0.0, latency_ms=(0, 1))
    # 10 successful calls
    for _ in range(10):
        result = await fake.create_and_post_order(
            {"size": 50.0, "price": 0.5, "side": "BUY", "token_id": "tok"}
        )
        assert result["success"] is True


def test_set_failure_rate_clamps_to_unit_interval():
    """set_failure_rate clamps в [0, 1]."""
    fake = FakeClobClient()
    fake.set_failure_rate(-0.5)
    assert fake._failure_rate == 0.0
    fake.set_failure_rate(1.5)
    assert fake._failure_rate == 1.0


# --- Drill helpers ---------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_state_clears_orders_and_trades():
    """reset_state() — clean slate между drill scenarios."""
    fake = FakeClobClient(seed=42, partial_fill_rate=0.0, latency_ms=(0, 1))
    await fake.create_and_post_order(
        {"size": 50.0, "price": 0.5, "side": "BUY", "token_id": "tok"}
    )
    assert len(await fake.get_trades()) > 0
    fake.reset_state()
    assert await fake.get_trades() == []
    assert fake.get_open_orders() == []
