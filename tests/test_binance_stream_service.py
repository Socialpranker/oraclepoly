"""Тесты для app/services/binance_stream_service.py.

Покрытие:
- _parse_trade корректно извлекает price из Binance trade event.
- _handle_message с combined-stream wrapper.
- _handle_message с raw trade payload (legacy form).
- Stale tick → get_latest возвращает None.
- Singleton.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from app.services import binance_stream_service as bsvc

# ---------- _parse_trade ----------


def test_parse_trade_valid():
    data = {"e": "trade", "E": 1779354900_000, "s": "BTCUSDT", "p": "77123.45", "q": "0.05"}
    tick = bsvc.BinanceStreamService._parse_trade(data, "BTC/USD")
    assert tick is not None
    assert tick.symbol == "BTC/USD"
    assert tick.price == pytest.approx(77123.45)
    assert tick.trade_time_ms == 1779354900_000


def test_parse_trade_missing_price_returns_none():
    data = {"e": "trade", "E": 1779354900_000, "s": "BTCUSDT"}
    assert bsvc.BinanceStreamService._parse_trade(data, "BTC/USD") is None


def test_parse_trade_invalid_payload_returns_none():
    assert bsvc.BinanceStreamService._parse_trade("not-a-dict", "BTC/USD") is None
    assert bsvc.BinanceStreamService._parse_trade({}, "BTC/USD") is None


# ---------- _handle_message ----------


def test_handle_combined_stream_message_populates_cache():
    svc = bsvc.BinanceStreamService()
    raw = (
        '{"stream":"btcusdt@trade",'
        '"data":{"e":"trade","E":1779354900000,"s":"BTCUSDT","p":"77100.00","q":"0.1"}}'
    )
    svc._handle_message(raw)
    tick = svc._cache.get("BTC/USD")
    assert tick is not None
    assert tick.price == pytest.approx(77100.00)


def test_handle_raw_trade_message_populates_via_s_field():
    """Если придёт raw trade без stream wrapper — fallback через s field."""
    svc = bsvc.BinanceStreamService()
    raw = '{"e":"trade","E":1779354900000,"s":"ETHUSDT","p":"3900.50","q":"0.5"}'
    svc._handle_message(raw)
    tick = svc._cache.get("ETH/USD")
    assert tick is not None
    assert tick.price == pytest.approx(3900.50)


def test_handle_unknown_symbol_is_ignored():
    svc = bsvc.BinanceStreamService()
    raw = '{"stream":"unknown@trade","data":{"e":"trade","E":1,"s":"DOGEUSDT","p":"0.1"}}'
    svc._handle_message(raw)
    assert "DOGE/USD" not in svc._cache


def test_handle_invalid_json_safe():
    svc = bsvc.BinanceStreamService()
    svc._handle_message("not-a-json")
    assert svc._cache == {}


# ---------- get_latest ----------


def test_get_latest_returns_fresh_tick():
    svc = bsvc.BinanceStreamService()
    now_ms = int(time.time() * 1000)
    svc._cache["BTC/USD"] = bsvc.BinanceTick(
        symbol="BTC/USD", price=77000.0, trade_time_ms=now_ms - 100, received_at_ms=now_ms - 100
    )
    tick = svc.get_latest("BTC/USD")
    assert tick is not None
    assert tick.price == pytest.approx(77000.0)


def test_get_latest_returns_none_when_stale():
    svc = bsvc.BinanceStreamService(stale_after_s=1.0)
    old_ms = int(time.time() * 1000) - 5_000  # 5s old
    svc._cache["BTC/USD"] = bsvc.BinanceTick(
        symbol="BTC/USD", price=77000.0, trade_time_ms=old_ms, received_at_ms=old_ms
    )
    assert svc.get_latest("BTC/USD") is None


def test_get_latest_missing_symbol():
    svc = bsvc.BinanceStreamService()
    assert svc.get_latest("BTC/USD") is None


# ---------- Singleton ----------


def test_singleton_returns_same_instance():
    bsvc._singleton = None
    s1 = bsvc.get_binance_stream()
    s2 = bsvc.get_binance_stream()
    assert s1 is s2


# ---------- start/stop idempotency ----------


@pytest.mark.asyncio
async def test_start_stop_lifecycle():
    """start → создаёт task; stop → cancel'ит task."""
    svc = bsvc.BinanceStreamService()
    with patch.object(svc, "_stream_loop", new=AsyncMock(side_effect=lambda: None)):
        await svc.start()
        await svc.stop()
    assert svc._task is None


@pytest.mark.asyncio
async def test_start_idempotent():
    """Двойной start не создаёт второй task."""
    svc = bsvc.BinanceStreamService()
    with patch.object(svc, "_stream_loop", new=AsyncMock(side_effect=lambda: None)):
        await svc.start()
        first_task = svc._task
        await svc.start()
        assert svc._task is first_task or svc._task is None  # либо тот же, либо уже done
        await svc.stop()
