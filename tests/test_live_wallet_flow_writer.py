"""Unit tests для live_wallet_flow_writer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.jobs.live_wallet_flow_writer import _aggregate_wallet_flow


def _trade(ts: int, taker: str, maker: str, side: str, size: float, price: float) -> dict:
    return {
        "timestamp": ts,
        "proxyWallet": taker,
        "maker": maker,
        "side": side,
        "size": size,
        "price": price,
    }


def test_empty_input() -> None:
    assert _aggregate_wallet_flow([], smart_wallets=set()) == {}


def test_classifies_takers_into_whale_vs_retail() -> None:
    ts = int(datetime(2026, 5, 5, 10, 0, tzinfo=UTC).timestamp())
    smart = {"0xwhale1", "0xwhale2"}
    trades = [
        _trade(ts, "0xwhale1", "0xmaker_a", "BUY", 100, 0.5),  # vol 50, whale
        _trade(ts, "0xretail1", "0xmaker_b", "BUY", 50, 0.4),  # vol 20, retail
        _trade(ts, "0xretail2", "0xmaker_a", "SELL", 80, 0.6),  # vol 48, retail
    ]
    flow = _aggregate_wallet_flow(trades, smart)
    assert len(flow) == 1
    f = next(iter(flow.values()))
    assert f["n_unique_takers"] == 3
    assert f["n_unique_makers"] == 2
    assert f["whale_taker_count"] == 1
    assert f["whale_taker_volume"] == 50.0
    # whale_buy_pressure = (50 - 0) / 50 = 1.0 (whale only buys)
    assert f["whale_buy_pressure"] == 1.0
    # retail avg: (20 + 48) / 2 = 34.0
    assert f["retail_taker_avg_size"] == 34.0
    assert f["taker_count_total"] == 3


def test_multiple_trades_same_whale_unique_count_correct() -> None:
    ts = int(datetime(2026, 5, 5, 10, 0, tzinfo=UTC).timestamp())
    smart = {"0xwhale1"}
    trades = [
        _trade(ts, "0xwhale1", "0xm", "BUY", 100, 0.5),
        _trade(ts, "0xwhale1", "0xm", "SELL", 100, 0.5),  # same whale
    ]
    flow = _aggregate_wallet_flow(trades, smart)
    f = next(iter(flow.values()))
    assert f["whale_taker_count"] == 1  # unique
    assert f["whale_taker_volume"] == 100.0  # 50 + 50
    assert f["taker_count_total"] == 2  # total trade count


def test_whale_buy_pressure_negative_when_net_selling() -> None:
    ts = int(datetime(2026, 5, 5, 10, 0, tzinfo=UTC).timestamp())
    smart = {"0xwhale1"}
    trades = [
        _trade(ts, "0xwhale1", "0xm", "SELL", 200, 0.5),  # vol 100 sell
        _trade(ts, "0xwhale1", "0xm", "BUY", 50, 0.5),  # vol 25 buy
    ]
    flow = _aggregate_wallet_flow(trades, smart)
    f = next(iter(flow.values()))
    # buy_pressure = (25 - 100) / 125 = -0.6
    assert f["whale_buy_pressure"] == -0.6


def test_retail_avg_none_when_no_retail() -> None:
    ts = int(datetime(2026, 5, 5, 10, 0, tzinfo=UTC).timestamp())
    smart = {"0xwhale1"}
    trades = [
        _trade(ts, "0xwhale1", "0xm", "BUY", 100, 0.5),
    ]
    flow = _aggregate_wallet_flow(trades, smart)
    f = next(iter(flow.values()))
    assert f["retail_taker_avg_size"] is None


def test_no_makers_when_field_missing() -> None:
    ts = int(datetime(2026, 5, 5, 10, 0, tzinfo=UTC).timestamp())
    trades = [
        {
            "timestamp": ts,
            "proxyWallet": "0xtaker",
            "side": "BUY",
            "size": 100,
            "price": 0.5,
            # no maker field
        },
    ]
    flow = _aggregate_wallet_flow(trades, smart_wallets=set())
    f = next(iter(flow.values()))
    assert f["n_unique_makers"] == 0
    assert f["n_unique_takers"] == 1


def test_invalid_trades_skipped() -> None:
    ts = int(datetime(2026, 5, 5, 10, 0, tzinfo=UTC).timestamp())
    trades = [
        _trade(ts, "", "0xm", "BUY", 100, 0.5),  # empty taker
        _trade(ts, "0xt", "0xm", "BUY", 0, 0.5),  # zero size
        _trade(ts, "0xt", "0xm", "BUY", 100, 0),  # zero price
        _trade(ts, "0xtaker", "0xm", "BUY", 100, 0.5),  # valid
    ]
    flow = _aggregate_wallet_flow(trades, smart_wallets=set())
    f = next(iter(flow.values()))
    assert f["taker_count_total"] == 1


@pytest.mark.asyncio
async def test_run_returns_zero_when_no_active(monkeypatch) -> None:
    from app.jobs import live_wallet_flow_writer as mod

    async def fake_fetch(query, *args, **kwargs):
        return []

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    result = await mod.run_live_wallet_flow_writer()
    assert result["markets"] == 0
    assert result["buckets_upserted"] == 0
