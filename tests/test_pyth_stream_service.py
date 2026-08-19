"""Tests для PythStreamService — pure-функции parse, slug resolution, cache."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from app.services.pyth_stream_service import (
    FEEDS,
    PythStreamService,
    PythTick,
    get_pyth_stream,
    symbol_for_slug,
)


class TestSlugSymbol:
    """symbol_for_slug — coin → Pyth symbol."""

    def test_btc_5m(self) -> None:
        assert symbol_for_slug("btc-updown-5m-1779354900") == "BTC/USD"

    def test_eth_15m(self) -> None:
        assert symbol_for_slug("eth-updown-15m-1779354900") == "ETH/USD"

    def test_sol_4h(self) -> None:
        assert symbol_for_slug("sol-updown-4h-1779336000") == "SOL/USD"

    def test_xrp_4h(self) -> None:
        assert symbol_for_slug("xrp-updown-4h-1779336000") == "XRP/USD"

    def test_uppercase_slug_handled(self) -> None:
        """Polymarket slugs lower-case, но защитимся от mixed-case."""
        assert symbol_for_slug("BTC-updown-5m-1779354900") == "BTC/USD"

    def test_non_updown_returns_none(self) -> None:
        """Slug без 'updown' — не наш detector."""
        assert symbol_for_slug("trump-second-term-2026") is None

    def test_unsupported_coin_returns_none(self) -> None:
        """ADA, DOGE и др. не в Pyth FEEDS — None."""
        assert symbol_for_slug("ada-updown-5m-1779354900") is None
        assert symbol_for_slug("doge-updown-15m-1779354900") is None

    def test_empty_or_none(self) -> None:
        assert symbol_for_slug("") is None
        assert symbol_for_slug(None) is None  # type: ignore[arg-type]


class TestParseEntry:
    """_parse_entry — Pyth message JSON → PythTick."""

    def _make_entry(self, feed_id_hex: str, mantissa: int, expo: int, pt: int) -> dict:
        """Helper — конструирует Pyth-style entry."""
        return {
            "id": feed_id_hex,
            "price": {"price": str(mantissa), "expo": expo, "publish_time": pt, "conf": "100"},
        }

    def test_real_btc_sample(self) -> None:
        """Реальный sample из 2026-05-21 prod smoke: BTC = $77,136."""
        entry = self._make_entry(
            "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
            7713600504000,  # mantissa
            -8,  # expo (Pyth standard)
            1779360000,  # publish_time (sec)
        )
        tick = PythStreamService._parse_entry(entry, now_ms=1779360500_000)
        assert tick is not None
        assert tick.symbol == "BTC/USD"
        assert tick.price == pytest.approx(77136.00504, rel=1e-6)
        assert tick.publish_time_ms == 1779360000_000
        assert tick.received_at_ms == 1779360500_000

    def test_eth_with_0x_prefix(self) -> None:
        """Pyth обычно возвращает feed id без 0x prefix — наш парсер должен это терпеть."""
        entry = self._make_entry(
            "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
            211200000000,
            -8,
            1779360000,
        )
        tick = PythStreamService._parse_entry(entry, now_ms=0)
        assert tick is not None
        assert tick.symbol == "ETH/USD"
        assert tick.price == pytest.approx(2112.0, abs=0.01)

    def test_unknown_feed_id_returns_none(self) -> None:
        """Неизвестный feed (не в FEEDS) — игнорируем."""
        entry = self._make_entry("dead" * 16, 1000, -2, 0)
        assert PythStreamService._parse_entry(entry, now_ms=0) is None

    def test_missing_price_block(self) -> None:
        entry = {"id": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"}
        assert PythStreamService._parse_entry(entry, now_ms=0) is None

    def test_invalid_price_values(self) -> None:
        entry = {
            "id": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
            "price": {"price": "not-a-number", "expo": -8, "publish_time": 0},
        }
        assert PythStreamService._parse_entry(entry, now_ms=0) is None

    def test_confidence_parsed(self) -> None:
        entry = self._make_entry(
            "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
            7713600504000,
            -8,
            1779360000,
        )
        tick = PythStreamService._parse_entry(entry, now_ms=0)
        # conf=100, expo=-8 → 100e-8 = 1e-6 USD
        assert tick is not None
        assert tick.confidence == pytest.approx(1e-6, rel=1e-9)


class TestCache:
    """In-memory cache + stale detection."""

    def test_get_latest_returns_none_when_empty(self) -> None:
        svc = PythStreamService()
        assert svc.get_latest("BTC/USD") is None

    def test_handle_message_populates_cache(self) -> None:
        svc = PythStreamService()
        feed_id = FEEDS["BTC/USD"][2:]  # без 0x prefix как Pyth обычно отдаёт
        msg = json.dumps(
            {
                "parsed": [
                    {
                        "id": feed_id,
                        "price": {
                            "price": "7713600504000",
                            "expo": -8,
                            "publish_time": int(time.time()),
                            "conf": "100",
                        },
                    }
                ]
            }
        )
        svc._handle_message(msg)
        tick = svc.get_latest("BTC/USD")
        assert tick is not None
        assert tick.symbol == "BTC/USD"
        assert tick.price == pytest.approx(77136.00504, rel=1e-6)

    def test_get_latest_returns_none_when_stale(self) -> None:
        """Если tick старше stale_after_s — get_latest вернёт None."""
        svc = PythStreamService(stale_after_s=10.0)
        old_tick = PythTick(
            symbol="BTC/USD",
            price=77000.0,
            publish_time_ms=0,
            received_at_ms=int(time.time() * 1000) - 30_000,  # 30s назад
        )
        svc._cache["BTC/USD"] = old_tick
        assert svc.get_latest("BTC/USD") is None

    def test_all_latest_filters_stale(self) -> None:
        svc = PythStreamService(stale_after_s=10.0)
        now_ms = int(time.time() * 1000)
        svc._cache["BTC/USD"] = PythTick("BTC/USD", 77000.0, 0, now_ms - 5_000)  # свежий
        svc._cache["ETH/USD"] = PythTick("ETH/USD", 2112.0, 0, now_ms - 30_000)  # stale
        result = svc.all_latest()
        assert set(result.keys()) == {"BTC/USD"}

    def test_handle_invalid_json_safe(self) -> None:
        """Невалидный JSON не падает с exception."""
        svc = PythStreamService()
        svc._handle_message("not-a-json")
        svc._handle_message('{"missing": "parsed"}')
        # cache пустой, нет crash
        assert svc.all_latest() == {}


class TestSingleton:
    """get_pyth_stream — lazy singleton."""

    def test_returns_same_instance(self) -> None:
        with patch("app.services.pyth_stream_service._singleton", None):
            s1 = get_pyth_stream()
            s2 = get_pyth_stream()
            assert s1 is s2
