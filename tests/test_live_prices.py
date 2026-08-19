"""Tests for app.services.live_prices.get_live_yes_price.

State-кэш семантика (fix 2026-07-03): midpoint из WS-кэша считается живым только
если снапшот получен в текущей эпохе WS-соединения (ws:conn_epoch) И heartbeat
ws:last_message_at свежее 120с. Оба гейта fail-closed → db_price fallback.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

EPOCH = 1751500000.123


def make_cache(micro: dict | None, epoch_rec: dict | None, heartbeat: dict | None) -> AsyncMock:
    """cache_get с раздачей по ключу: microstructure / ws:conn_epoch / ws:last_message_at."""
    data = {
        "microstructure:tok1": micro,
        "ws:conn_epoch": epoch_rec,
        "ws:last_message_at": heartbeat,
    }
    return AsyncMock(side_effect=lambda key: data.get(key))


def fresh_env(micro: dict | None) -> AsyncMock:
    """Кэш с валидной эпохой и свежим heartbeat — изолирует тесты midpoint-логики."""
    return make_cache(micro, {"epoch": EPOCH}, {"ts": time.time()})


class TestIsWsSnapshotLive:
    """Общий гейт свежести для всех читателей microstructure-кэша."""

    @pytest.mark.asyncio
    async def test_true_when_epoch_matches_and_heartbeat_fresh(self):
        from app.services.live_prices import is_ws_snapshot_live

        cache = make_cache(None, {"epoch": EPOCH}, {"ts": time.time()})
        with patch("app.services.live_prices.cache_get", cache):
            assert await is_ws_snapshot_live({"epoch": EPOCH}) is True

    @pytest.mark.asyncio
    async def test_false_when_epoch_mismatch(self):
        from app.services.live_prices import is_ws_snapshot_live

        cache = make_cache(None, {"epoch": EPOCH}, {"ts": time.time()})
        with patch("app.services.live_prices.cache_get", cache):
            assert await is_ws_snapshot_live({"epoch": EPOCH - 1}) is False

    @pytest.mark.asyncio
    async def test_false_when_payload_none_or_without_epoch(self):
        from app.services.live_prices import is_ws_snapshot_live

        cache = make_cache(None, {"epoch": EPOCH}, {"ts": time.time()})
        with patch("app.services.live_prices.cache_get", cache):
            assert await is_ws_snapshot_live(None) is False
            assert await is_ws_snapshot_live({"midpoint": 0.5}) is False

    @pytest.mark.asyncio
    async def test_false_when_heartbeat_stale(self):
        from app.services.live_prices import is_ws_snapshot_live

        cache = make_cache(None, {"epoch": EPOCH}, {"ts": time.time() - 500})
        with patch("app.services.live_prices.cache_get", cache):
            assert await is_ws_snapshot_live({"epoch": EPOCH}) is False


class TestGetLiveYesPrice:
    @pytest.mark.asyncio
    async def test_returns_live_midpoint_when_epoch_matches_and_heartbeat_fresh(self):
        from app.services.live_prices import get_live_yes_price

        cache = fresh_env({"midpoint": 0.42, "epoch": EPOCH})
        with patch("app.services.live_prices.cache_get", cache):
            price, is_live = await get_live_yes_price("tok1", 0.50)

        assert price == 0.42
        assert is_live is True

    @pytest.mark.asyncio
    async def test_falls_back_when_snapshot_from_previous_connection_epoch(self):
        from app.services.live_prices import get_live_yes_price

        cache = make_cache(
            {"midpoint": 0.42, "epoch": EPOCH - 100},
            {"epoch": EPOCH},
            {"ts": time.time()},
        )
        with patch("app.services.live_prices.cache_get", cache):
            price, is_live = await get_live_yes_price("tok1", 0.50)

        assert price == 0.50
        assert is_live is False

    @pytest.mark.asyncio
    async def test_falls_back_when_snapshot_has_no_epoch_field(self):
        """Ключи, записанные кодом до фикса (без epoch), не считаются live."""
        from app.services.live_prices import get_live_yes_price

        cache = fresh_env({"midpoint": 0.42})
        with patch("app.services.live_prices.cache_get", cache):
            price, is_live = await get_live_yes_price("tok1", 0.50)

        assert price == 0.50
        assert is_live is False

    @pytest.mark.asyncio
    async def test_falls_back_when_conn_epoch_key_missing(self):
        from app.services.live_prices import get_live_yes_price

        cache = make_cache({"midpoint": 0.42, "epoch": EPOCH}, None, {"ts": time.time()})
        with patch("app.services.live_prices.cache_get", cache):
            price, is_live = await get_live_yes_price("tok1", 0.50)

        assert price == 0.50
        assert is_live is False

    @pytest.mark.asyncio
    async def test_falls_back_when_heartbeat_stale(self):
        from app.services.live_prices import get_live_yes_price

        cache = make_cache(
            {"midpoint": 0.42, "epoch": EPOCH},
            {"epoch": EPOCH},
            {"ts": time.time() - 500},
        )
        with patch("app.services.live_prices.cache_get", cache):
            price, is_live = await get_live_yes_price("tok1", 0.50)

        assert price == 0.50
        assert is_live is False

    @pytest.mark.asyncio
    async def test_falls_back_when_heartbeat_missing(self):
        from app.services.live_prices import get_live_yes_price

        cache = make_cache({"midpoint": 0.42, "epoch": EPOCH}, {"epoch": EPOCH}, None)
        with patch("app.services.live_prices.cache_get", cache):
            price, is_live = await get_live_yes_price("tok1", 0.50)

        assert price == 0.50
        assert is_live is False

    @pytest.mark.asyncio
    async def test_falls_back_to_db_price_when_cache_empty(self):
        from app.services.live_prices import get_live_yes_price

        with patch("app.services.live_prices.cache_get", fresh_env(None)):
            price, is_live = await get_live_yes_price("tok1", 0.50)

        assert price == 0.50
        assert is_live is False

    @pytest.mark.asyncio
    async def test_falls_back_to_db_price_when_midpoint_is_garbage(self):
        from app.services.live_prices import get_live_yes_price

        cache = fresh_env({"midpoint": "not-a-number", "epoch": EPOCH})
        with patch("app.services.live_prices.cache_get", cache):
            price, is_live = await get_live_yes_price("tok1", 0.50)

        assert price == 0.50
        assert is_live is False

    @pytest.mark.asyncio
    async def test_falls_back_when_midpoint_out_of_range(self):
        from app.services.live_prices import get_live_yes_price

        cache = fresh_env({"midpoint": 1.5, "epoch": EPOCH})
        with patch("app.services.live_prices.cache_get", cache):
            price, is_live = await get_live_yes_price("tok1", 0.50)

        assert price == 0.50
        assert is_live is False

    @pytest.mark.asyncio
    async def test_no_token_id_returns_db_price_without_cache_call(self):
        from app.services.live_prices import get_live_yes_price

        mock_cache_get = fresh_env({"midpoint": 0.42, "epoch": EPOCH})
        with patch("app.services.live_prices.cache_get", mock_cache_get):
            price, is_live = await get_live_yes_price(None, 0.50)

        assert price == 0.50
        assert is_live is False
        mock_cache_get.assert_not_called()


class TestRestSeed:
    """REST-посев книг для рынков без WS-тиков (позиции). Измерено 2026-07-03:
    Polymarket НЕ шлёт массовый initial dump — 169 ключей за 4ч при 10k подписок,
    live_coverage(mtm) остался 0%. Посев валиден ограниченно: TTL ключа."""

    @pytest.mark.asyncio
    async def test_rest_seed_payload_is_live_without_epoch(self):
        from app.services.live_prices import get_live_yes_price, is_ws_snapshot_live

        seed_payload = {"midpoint": 0.42, "source": "rest_seed"}
        cache = make_cache(seed_payload, None, None)  # epoch/heartbeat ключей нет вовсе
        with patch("app.services.live_prices.cache_get", cache):
            assert await is_ws_snapshot_live(seed_payload) is True
            price, is_live = await get_live_yes_price("tok1", 0.50)

        assert price == 0.42
        assert is_live is True

    @pytest.mark.asyncio
    async def test_epoch_takes_precedence_over_rest_seed_marker(self):
        """Merged payload (посев + WS-partial со старой эпохой) судится по epoch."""
        from app.services.live_prices import is_ws_snapshot_live

        cache = make_cache(None, {"epoch": EPOCH}, {"ts": time.time()})
        with patch("app.services.live_prices.cache_get", cache):
            assert await is_ws_snapshot_live({"source": "rest_seed", "epoch": EPOCH - 1}) is False

    @pytest.mark.asyncio
    async def test_seed_book_snapshot_writes_seed_payload(self):
        from app.services.live_prices import REST_SEED_TTL_SEC, seed_book_snapshot

        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        book = {
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.44", "size": "50"}],
        }
        with (
            patch("app.core.cache.get_redis", AsyncMock(return_value=redis)),
            patch(
                "app.services.polymarket_poller_service.PolymarketPollerService.fetch_order_book_rest",
                new_callable=AsyncMock,
                return_value=book,
            ),
            patch("app.services.live_prices.cache_set", new_callable=AsyncMock) as mock_set,
        ):
            ok = await seed_book_snapshot("tokS")

        assert ok is True
        call = mock_set.call_args_list[0]
        assert call[0][0] == "microstructure:tokS"
        payload = call[0][1]
        assert payload["source"] == "rest_seed"
        assert payload["midpoint"] == pytest.approx(0.42)
        assert call[1]["ttl_seconds"] == REST_SEED_TTL_SEC

    @pytest.mark.asyncio
    async def test_seed_book_snapshot_throttled_skips_rest(self):
        from app.services.live_prices import seed_book_snapshot

        redis = AsyncMock()
        redis.set = AsyncMock(return_value=None)  # SETNX проигран — недавно сеяли
        rest = AsyncMock()
        with (
            patch("app.core.cache.get_redis", AsyncMock(return_value=redis)),
            patch("app.services.polymarket_poller_service.PolymarketPollerService.fetch_order_book_rest", rest),
        ):
            assert await seed_book_snapshot("tokS") is False
        rest.assert_not_called()

    @pytest.mark.asyncio
    async def test_seed_book_snapshot_empty_book_returns_false(self):
        from app.services.live_prices import seed_book_snapshot

        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        with (
            patch("app.core.cache.get_redis", AsyncMock(return_value=redis)),
            patch(
                "app.services.polymarket_poller_service.PolymarketPollerService.fetch_order_book_rest",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.services.live_prices.cache_set", new_callable=AsyncMock) as mock_set,
        ):
            assert await seed_book_snapshot("tokS") is False
        mock_set.assert_not_called()


class TestMtmSeedHook:
    @pytest.mark.asyncio
    async def test_seeds_only_when_price_not_live(self):
        from app.jobs.mtm_refresh import _maybe_seed_position_book

        row = {"yes_token_id": "tokM", "current_price": 0.5}
        with (
            patch("app.jobs.mtm_refresh.get_live_yes_price", AsyncMock(return_value=(0.5, False))),
            patch("app.jobs.mtm_refresh.seed_book_snapshot", new_callable=AsyncMock) as seed,
        ):
            await _maybe_seed_position_book(row)
        seed.assert_awaited_once_with("tokM")

        with (
            patch("app.jobs.mtm_refresh.get_live_yes_price", AsyncMock(return_value=(0.5, True))),
            patch("app.jobs.mtm_refresh.seed_book_snapshot", new_callable=AsyncMock) as seed,
        ):
            await _maybe_seed_position_book(row)
        seed.assert_not_awaited()
