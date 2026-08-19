"""Тесты для app/services/pyth_history_service.py.

Покрытие:
- Unknown symbol → None без HTTP.
- Cache hit → нет HTTP запроса.
- Successful fetch → cache set + return price.
- HTTP error → None, no cache.
- Bad parse (no 'parsed') → None.
- Mismatched feed_id → None.
- _parse_response: 0x-prefixed vs raw IDs обе работают.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import pyth_history_service as hist


@pytest.fixture(autouse=True)
def _reset_client():
    """Сбрасываем singleton client между тестами."""
    hist._client = None
    yield
    hist._client = None


# ---------- _parse_response ----------


def test_parse_response_btc_with_0x_prefix():
    payload = {
        "parsed": [
            {
                "id": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
                "price": {"price": "7700000000000", "expo": -8, "publish_time": 1779354600},
            }
        ]
    }
    assert hist._parse_response(payload, "BTC/USD") == pytest.approx(77000.0)


def test_parse_response_btc_without_0x_prefix():
    """Pyth REST иногда отдаёт ids без 0x — должно работать."""
    payload = {
        "parsed": [
            {
                "id": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
                "price": {"price": "7700000000000", "expo": -8, "publish_time": 1779354600},
            }
        ]
    }
    assert hist._parse_response(payload, "BTC/USD") == pytest.approx(77000.0)


def test_parse_response_no_match_returns_none():
    """Wrong feed_id → None."""
    payload = {
        "parsed": [
            {
                "id": "0xdeadbeef",
                "price": {"price": "100", "expo": -2, "publish_time": 1},
            }
        ]
    }
    assert hist._parse_response(payload, "BTC/USD") is None


def test_parse_response_missing_parsed_returns_none():
    assert hist._parse_response({}, "BTC/USD") is None
    assert hist._parse_response({"parsed": None}, "BTC/USD") is None


def test_parse_response_unknown_symbol_returns_none():
    payload = {"parsed": []}
    assert hist._parse_response(payload, "UNKNOWN/USD") is None


def test_parse_response_malformed_price_returns_none():
    payload = {
        "parsed": [
            {
                "id": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
                "price": {"price": "not-a-number", "expo": -8},
            }
        ]
    }
    assert hist._parse_response(payload, "BTC/USD") is None


# ---------- get_price_at ----------


@pytest.mark.asyncio
async def test_get_price_unknown_symbol():
    result = await hist.get_price_at("DOGE/USD", 1779354600)
    assert result is None


@pytest.mark.asyncio
async def test_get_price_cache_hit_skips_http():
    """Cache hit → нет HTTP."""
    cached = {"price": 77000.0}
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=AssertionError("should NOT call HTTP"))

    with (
        patch("app.services.pyth_history_service.cache_get", AsyncMock(return_value=cached)),
        patch(
            "app.services.pyth_history_service._get_client",
            AsyncMock(return_value=fake_client),
        ),
    ):
        result = await hist.get_price_at("BTC/USD", 1779354600)
    assert result == pytest.approx(77000.0)


@pytest.mark.asyncio
async def test_get_price_cache_miss_fetches_and_caches():
    """Cache miss → HTTP fetch + cache_set."""
    payload = {
        "parsed": [
            {
                "id": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
                "price": {"price": "7700000000000", "expo": -8, "publish_time": 1779354600},
            }
        ]
    }
    fake_resp = AsyncMock()
    fake_resp.raise_for_status = lambda: None
    fake_resp.json = lambda: payload
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_resp)

    set_mock = AsyncMock()
    with (
        patch("app.services.pyth_history_service.cache_get", AsyncMock(return_value=None)),
        patch("app.services.pyth_history_service.cache_set", set_mock),
        patch(
            "app.services.pyth_history_service._get_client",
            AsyncMock(return_value=fake_client),
        ),
    ):
        result = await hist.get_price_at("BTC/USD", 1779354600)

    assert result == pytest.approx(77000.0)
    set_mock.assert_awaited_once()
    args, kwargs = set_mock.call_args
    assert args[0] == "pyth:hist:BTC/USD:1779354600"
    assert args[1] == {"price": 77000.0}


@pytest.mark.asyncio
async def test_get_price_http_error_returns_none():
    """HTTPError → None, без crash."""
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=httpx.RequestError("network down"))

    with (
        patch("app.services.pyth_history_service.cache_get", AsyncMock(return_value=None)),
        patch(
            "app.services.pyth_history_service._get_client",
            AsyncMock(return_value=fake_client),
        ),
    ):
        result = await hist.get_price_at("BTC/USD", 1779354600)
    assert result is None


@pytest.mark.asyncio
async def test_get_price_corrupted_cache_refetches():
    """Cache хранит мусор → refetch без crash."""
    payload = {
        "parsed": [
            {
                "id": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
                "price": {"price": "7800000000000", "expo": -8, "publish_time": 1779354600},
            }
        ]
    }
    fake_resp = AsyncMock()
    fake_resp.raise_for_status = lambda: None
    fake_resp.json = lambda: payload
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_resp)

    with (
        patch(
            "app.services.pyth_history_service.cache_get",
            AsyncMock(return_value={"price": "garbage"}),
        ),
        patch("app.services.pyth_history_service.cache_set", AsyncMock()),
        patch(
            "app.services.pyth_history_service._get_client",
            AsyncMock(return_value=fake_client),
        ),
    ):
        result = await hist.get_price_at("BTC/USD", 1779354600)

    assert result == pytest.approx(78000.0)
    fake_client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_client_no_op_when_uninitialized():
    """close_client() безопасно если client ещё не создан."""
    await hist.close_client()  # ничего не должно ломаться
    assert hist._client is None
