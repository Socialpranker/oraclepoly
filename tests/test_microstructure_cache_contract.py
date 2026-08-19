"""Контракт: writers microstructure-кэша пишут все ключи, которые читает execution_safety.fetch_live_book.

Audit 2026-07-02 (топ-2): fetch_live_book требует best_bid_size/best_ask_size из
WS-кэша, но ни один writer их не писал → fast-path всегда промахивался в REST.
Плюс _handle_best_bid_ask перезаписывал ключ целиком, затирая depth/levels/size
от предыдущего полного book-сообщения.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.polymarket_ws_service import PolymarketWebSocketService

# Ключи, которые читает fetch_live_book (app/services/execution_safety.py, fetch_live_book).
# При изменении reader — обновить список сознательно.
READER_REQUIRED_KEYS = {"best_bid", "best_ask", "best_bid_size", "best_ask_size"}


def test_calculate_microstructure_covers_reader_keys():
    bids = [{"price": "0.48", "size": "120"}, {"price": "0.47", "size": "300"}]
    asks = [{"price": "0.52", "size": "90"}, {"price": "0.53", "size": "250"}]
    micro = PolymarketWebSocketService._calculate_microstructure(bids, asks)
    assert READER_REQUIRED_KEYS <= set(micro.keys())
    assert micro["best_ask_size"] == 90.0
    assert micro["best_bid_size"] == 120.0


@pytest.fixture
def ws_service():
    with patch("app.services.polymarket_ws_service.get_settings"):
        service = PolymarketWebSocketService()
    return service


@pytest.mark.asyncio
async def test_handle_best_bid_ask_merges_not_overwrites(ws_service):
    """best_bid_ask-событие не несёт size/depth/levels — должно merge-ить их
    поверх существующего кэша (от полного book-сообщения), а не затирать целиком.
    """
    token_id = "tok_merge_1"
    existing_cache = {
        "best_bid": 0.40,
        "best_ask": 0.41,
        "best_bid_size": 500.0,
        "best_ask_size": 600.0,
        "depth_bid": 5000.0,
        "depth_ask": 6000.0,
        "levels": {"bids": [{"price": 0.40, "size": 500.0}], "asks": [{"price": 0.41, "size": 600.0}]},
        "spread": 0.024,
        "health": "moderate",
    }

    message = {
        "asset_id": token_id,
        "best_bid": "0.50",
        "best_ask": "0.51",
    }

    with (
        patch(
            "app.services.polymarket_ws_service.cache_get", new_callable=AsyncMock, return_value=dict(existing_cache)
        ) as mock_cache_get,
        patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set,
    ):
        await ws_service._handle_best_bid_ask(message)

    mock_cache_get.assert_awaited()
    call_args = mock_cache_set.call_args_list
    assert len(call_args) >= 1
    written = call_args[0][0][1]

    # Новые best_bid/best_ask применились
    assert written["best_bid"] == 0.50
    assert written["best_ask"] == 0.51
    # Старые depth/levels/size НЕ затёрты
    assert written["best_bid_size"] == 500.0
    assert written["best_ask_size"] == 600.0
    assert written["depth_bid"] == 5000.0
    assert written["depth_ask"] == 6000.0
    assert written["levels"] == existing_cache["levels"]


# === Reader-гейт свежести (state-кэш 2026-07-03) ===
#
# TTL microstructure вырос 60с→6ч: «свежесть по TTL» больше не работает,
# читатели денежного пути обязаны проверять epoch+heartbeat через
# is_ws_snapshot_live, иначе REST fallback.


def _freshness_cache(extra: dict, *, epoch: float = 42.0, hb_age: float = 0.0):
    import time as _time

    data = {
        "ws:conn_epoch": {"epoch": 42.0},
        "ws:last_message_at": {"ts": _time.time() - hb_age},
        **extra,
    }
    return AsyncMock(side_effect=lambda key: data.get(key))


_WS_BOOK = {
    "best_bid": 0.40,
    "best_ask": 0.42,
    "best_bid_size": 100.0,
    "best_ask_size": 120.0,
}


@pytest.mark.asyncio
async def test_fetch_live_book_uses_fresh_ws_payload():
    from app.services.execution_safety import fetch_live_book

    cache = _freshness_cache({"microstructure:tokA": {**_WS_BOOK, "epoch": 42.0}})
    with (
        patch("app.core.cache.cache_get", cache),
        patch("app.services.live_prices.cache_get", cache),
    ):
        book = await fetch_live_book("tokA")

    assert book == {"bid": 0.40, "ask": 0.42, "bid_size": 100.0, "ask_size": 120.0}


@pytest.mark.asyncio
async def test_fetch_live_book_stale_epoch_falls_back_to_rest():
    """Снапшот прошлого коннекта не должен кормить live safety-гейты."""
    from app.services.execution_safety import fetch_live_book

    cache = _freshness_cache({"microstructure:tokA": {**_WS_BOOK, "epoch": 41.0}})
    rest_book = {
        "bids": [{"price": "0.30", "size": "50"}],
        "asks": [{"price": "0.33", "size": "60"}],
    }
    with (
        patch("app.core.cache.cache_get", cache),
        patch("app.services.live_prices.cache_get", cache),
        patch(
            "app.services.polymarket_poller_service.PolymarketPollerService.fetch_order_book_rest",
            new_callable=AsyncMock,
            return_value=rest_book,
        ),
    ):
        book = await fetch_live_book("tokA")

    assert book is not None
    assert book["bid"] == 0.30  # из REST, не из протухшего WS


@pytest.mark.asyncio
async def test_virtual_fetch_book_ws_only_rejects_stale_epoch():
    from app.services.virtual_execution import _fetch_book_ws_only

    stale = _freshness_cache({"microstructure:tokB": {**_WS_BOOK, "epoch": 41.0}})
    with (
        patch("app.core.cache.cache_get", stale),
        patch("app.services.live_prices.cache_get", stale),
    ):
        assert await _fetch_book_ws_only("tokB") is None

    fresh = _freshness_cache({"microstructure:tokB": {**_WS_BOOK, "epoch": 42.0}})
    with (
        patch("app.core.cache.cache_get", fresh),
        patch("app.services.live_prices.cache_get", fresh),
    ):
        book = await _fetch_book_ws_only("tokB")
    assert book is not None and book["bid"] == 0.40


@pytest.mark.asyncio
async def test_snapshot_writer_get_yes_price_rejects_stale_epoch():
    from app.jobs.live_price_snapshot_writer import _get_yes_price

    rest_book = {
        "bids": [{"price": "0.30", "size": "50"}],
        "asks": [{"price": "0.32", "size": "60"}],
    }
    stale = _freshness_cache({"microstructure:tokC": {**_WS_BOOK, "epoch": 41.0}})
    with (
        patch("app.jobs.live_price_snapshot_writer.cache_get", stale),
        patch("app.core.cache.cache_get", stale),
        patch("app.services.live_prices.cache_get", stale),
        patch(
            "app.services.polymarket_poller_service.PolymarketPollerService.fetch_order_book_rest",
            new_callable=AsyncMock,
            return_value=rest_book,
        ),
    ):
        mid = await _get_yes_price("tokC")

    assert mid == pytest.approx(0.31)  # REST mid, не WS 0.41
