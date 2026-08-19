"""Unit tests для live_trade_bucket_writer.

Фокус на _aggregate_to_buckets — pure функция over trades list.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.jobs.live_trade_bucket_writer import _aggregate_to_buckets


def _trade(ts: int, side: str, size: float, price: float) -> dict:
    return {"timestamp": ts, "side": side, "size": size, "price": price, "proxyWallet": "0xabc"}


def test_empty_trades_returns_empty_buckets() -> None:
    assert _aggregate_to_buckets([]) == {}


def test_single_trade_creates_one_bucket() -> None:
    # 2026-05-05 10:30 UTC → bucket = 10:00
    ts = int(datetime(2026, 5, 5, 10, 30, tzinfo=UTC).timestamp())
    trades = [_trade(ts, "BUY", size=100, price=0.5)]
    buckets = _aggregate_to_buckets(trades)
    assert len(buckets) == 1
    bucket_key = datetime(2026, 5, 5, 10, 0, tzinfo=UTC)
    assert bucket_key in buckets
    b = buckets[bucket_key]
    assert b["n_trades"] == 1
    assert b["total_volume_usd"] == 50.0  # 100 * 0.5
    assert b["vwap_yes"] == 0.5
    assert b["min_yes_price"] == 0.5
    assert b["max_yes_price"] == 0.5
    assert b["buy_volume"] == 50.0
    assert b["sell_volume"] == 0.0


def test_multiple_trades_in_same_hour_aggregate() -> None:
    base = int(datetime(2026, 5, 5, 10, 5, tzinfo=UTC).timestamp())
    trades = [
        _trade(base, "BUY", 100, 0.5),  # vol=50
        _trade(base + 60, "SELL", 200, 0.6),  # vol=120
        _trade(base + 600, "BUY", 50, 0.4),  # vol=20
    ]
    buckets = _aggregate_to_buckets(trades)
    assert len(buckets) == 1
    b = next(iter(buckets.values()))
    assert b["n_trades"] == 3
    # total vol = 50 + 120 + 20 = 190
    assert b["total_volume_usd"] == pytest.approx(190.0)
    # VWAP = (50*0.5 + 120*0.6 + 20*0.4) / 190 = (25 + 72 + 8) / 190 = 105/190
    assert b["vwap_yes"] == pytest.approx(105.0 / 190.0, abs=1e-5)
    assert b["min_yes_price"] == 0.4
    assert b["max_yes_price"] == 0.6
    assert b["buy_volume"] == pytest.approx(70.0)  # 50 + 20
    assert b["sell_volume"] == pytest.approx(120.0)


def test_trades_in_different_hours_create_separate_buckets() -> None:
    h10 = int(datetime(2026, 5, 5, 10, 30, tzinfo=UTC).timestamp())
    h11 = int(datetime(2026, 5, 5, 11, 15, tzinfo=UTC).timestamp())
    trades = [
        _trade(h10, "BUY", 100, 0.5),
        _trade(h11, "SELL", 200, 0.6),
    ]
    buckets = _aggregate_to_buckets(trades)
    assert len(buckets) == 2


def test_invalid_trades_skipped() -> None:
    """Trade with size=0, price=0, price>=1, missing fields → skipped."""
    ts = int(datetime(2026, 5, 5, 10, 0, tzinfo=UTC).timestamp())
    trades = [
        _trade(ts, "BUY", 0, 0.5),  # size=0
        _trade(ts, "BUY", 100, 0),  # price=0
        _trade(ts, "BUY", 100, 1.0),  # price=1 (out of range)
        {"timestamp": ts, "side": "BUY"},  # missing size/price
        _trade(ts, "BUY", 100, 0.5),  # valid
    ]
    buckets = _aggregate_to_buckets(trades)
    assert len(buckets) == 1
    b = next(iter(buckets.values()))
    assert b["n_trades"] == 1


def test_unknown_side_treated_as_neither_buy_nor_sell() -> None:
    """Side != BUY/SELL → vol учитывается в total, но не в buy/sell split."""
    ts = int(datetime(2026, 5, 5, 10, 0, tzinfo=UTC).timestamp())
    trades = [
        _trade(ts, "UNKNOWN", 100, 0.5),
        _trade(ts, "BUY", 100, 0.5),
    ]
    buckets = _aggregate_to_buckets(trades)
    b = next(iter(buckets.values()))
    assert b["n_trades"] == 2
    assert b["total_volume_usd"] == 100.0
    assert b["buy_volume"] == 50.0
    assert b["sell_volume"] == 0.0


@pytest.mark.asyncio
async def test_run_returns_zero_when_no_active_markets(monkeypatch) -> None:
    from app.jobs import live_trade_bucket_writer as mod

    async def fake_fetch(query, *args, **kwargs):
        return []

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    result = await mod.run_live_trade_bucket_writer()
    assert result == {"markets": 0, "trades_fetched": 0, "buckets_upserted": 0, "deleted_old": 0}
