"""
Тесты для PolymarketWebSocketService — WebSocket интеграция с CLOB.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.polymarket_ws_service import (
    MICROSTRUCTURE_STATE_TTL_SEC,
    PolymarketWebSocketService,
    get_market_microstructure,
)


@pytest.fixture
def ws_service():
    """Создаёт PolymarketWebSocketService с мок-настройками."""
    with patch("app.services.polymarket_ws_service.get_settings") as mock_settings:
        settings = MagicMock()
        settings.polymarket_ws_url = "wss://test.example.com/ws/market"
        mock_settings.return_value = settings
        service = PolymarketWebSocketService()
    return service


# === _calculate_microstructure ===


def test_calculate_microstructure_normal(ws_service):
    """Нормальный стакан → корректные L и s."""
    bids = [{"price": "0.55", "size": "1000"}, {"price": "0.54", "size": "500"}]
    asks = [{"price": "0.56", "size": "800"}, {"price": "0.57", "size": "300"}]

    result = ws_service._calculate_microstructure(bids, asks)

    assert result["spread"] == pytest.approx((0.56 - 0.55) / 0.56, rel=1e-4)
    assert result["depth_yes"] == 1500.0  # sum of bid sizes (YES side)
    assert result["depth_no"] == 1100.0  # sum of ask sizes (NO side)
    assert result["best_bid"] == 0.55
    assert result["best_ask"] == 0.56


def test_calculate_microstructure_empty(ws_service):
    """Пустой стакан → spread = 1.0, depth = 0."""
    result = ws_service._calculate_microstructure([], [])

    assert result["spread"] == 1.0
    assert result["depth_yes"] == 0.0
    assert result["depth_no"] == 0.0
    assert result["best_bid"] == 0.0
    assert result["best_ask"] == 0.0


def test_calculate_microstructure_only_bids(ws_service):
    """Только bids, нет asks → spread = 1.0 (нет ликвидности на стороне продажи)."""
    bids = [{"price": "0.50", "size": "2000"}]
    result = ws_service._calculate_microstructure(bids, [])

    assert result["spread"] == 1.0
    assert result["depth_yes"] == 2000.0
    assert result["depth_no"] == 0.0


def test_calculate_microstructure_only_asks(ws_service):
    """Только asks, нет bids → spread = 1.0."""
    asks = [{"price": "0.60", "size": "500"}]
    result = ws_service._calculate_microstructure([], asks)

    assert result["spread"] == 1.0
    assert result["depth_yes"] == 0.0
    assert result["depth_no"] == 500.0


def test_calculate_microstructure_tight_spread(ws_service):
    """Очень узкий spread → значение близко к 0."""
    bids = [{"price": "0.559", "size": "10000"}]
    asks = [{"price": "0.560", "size": "10000"}]

    result = ws_service._calculate_microstructure(bids, asks)

    assert result["spread"] == pytest.approx(0.001 / 0.560, rel=1e-3)
    assert result["spread"] < 0.002  # Очень узкий


def test_calculate_microstructure_list_format(ws_service):
    """Ордера в формате [[price, size]] тоже парсятся."""
    bids = [[0.55, 1000], [0.54, 500]]
    asks = [[0.56, 800]]

    result = ws_service._calculate_microstructure(bids, asks)

    assert result["depth_yes"] == 1500.0
    assert result["depth_no"] == 800.0
    assert result["best_bid"] == 0.55
    assert result["best_ask"] == 0.56


# === _handle_market_message ===


@pytest.mark.asyncio
async def test_handle_market_message_updates_cache(ws_service):
    """Сообщение с bids/asks → обновляет Redis cache по token_id."""
    message = {
        "market": "token_123",
        "bids": [{"price": "0.55", "size": "1000"}],
        "asks": [{"price": "0.56", "size": "800"}],
    }

    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set:
        await ws_service._handle_market_message(message)

    # Без маппинга — два вызова: microstructure:{token_id} + orderbook:{token_id}
    # (+ heartbeat "ws:last_message_at", исключён отдельно — не относится к book-данным)
    calls = [c for c in mock_cache_set.call_args_list if c[0][0] != "ws:last_message_at"]
    assert len(calls) == 2
    keys = [call[0][0] for call in calls]
    assert "microstructure:token_123" in keys
    assert "orderbook:token_123" in keys
    # Проверяем данные первого вызова (microstructure)
    call_args = calls[0]
    cached_data = call_args[0][1]
    assert cached_data["depth_yes"] == 1000.0
    assert cached_data["spread"] == pytest.approx((0.56 - 0.55) / 0.56, rel=1e-4)
    assert "updated_at" in cached_data
    # State-кэш: microstructure живёт долго (валидность решает epoch-гейт),
    # orderbook остаётся коротким (его money-path не читает).
    ttl_by_key = {c[0][0]: c[1]["ttl_seconds"] for c in calls}
    assert ttl_by_key["microstructure:token_123"] == MICROSTRUCTURE_STATE_TTL_SEC
    assert ttl_by_key["orderbook:token_123"] == 60


@pytest.mark.asyncio
async def test_handle_market_message_writes_both_keys_with_mapping(ws_service):
    """При наличии маппинга token_id → market_id пишет в оба ключа Redis."""
    market_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    await ws_service.subscribe_market("token_123", market_id=market_uuid)

    message = {
        "market": "token_123",
        "bids": [{"price": "0.55", "size": "1000"}],
        "asks": [{"price": "0.56", "size": "800"}],
    }

    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set:
        await ws_service._handle_market_message(message)

    # Четыре вызова: microstructure + orderbook для token_id и market_id
    # (+ heartbeat "ws:last_message_at", исключён отдельно)
    calls = [c for c in mock_cache_set.call_args_list if c[0][0] != "ws:last_message_at"]
    assert len(calls) == 4
    keys = [call[0][0] for call in calls]
    assert "microstructure:token_123" in keys
    assert "orderbook:token_123" in keys
    assert f"microstructure:{market_uuid}" in keys
    assert f"orderbook:{market_uuid}" in keys


@pytest.mark.asyncio
async def test_handle_market_message_stamps_conn_epoch(ws_service):
    """Снапшот несёт эпоху соединения — get_live_yes_price отсекает данные прошлых коннектов."""
    ws_service._conn_epoch = 1751500000.5
    message = {
        "market": "token_123",
        "bids": [{"price": "0.55", "size": "1000"}],
        "asks": [{"price": "0.56", "size": "800"}],
    }

    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set:
        await ws_service._handle_market_message(message)

    calls = [c for c in mock_cache_set.call_args_list if c[0][0] == "microstructure:token_123"]
    assert calls[0][0][1]["epoch"] == 1751500000.5


@pytest.mark.asyncio
async def test_best_bid_ask_stamps_epoch_and_state_ttl(ws_service):
    """Partial-апдейт тоже штампует эпоху и state-TTL (merge не должен терять гейт)."""
    ws_service._conn_epoch = 1751500000.5
    message = {
        "type": "best_bid_ask",
        "asset_id": "token_123",
        "best_bid": "0.55",
        "best_ask": "0.56",
    }

    with (
        patch("app.services.polymarket_ws_service.cache_get", AsyncMock(return_value=None)),
        patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set,
    ):
        await ws_service._handle_best_bid_ask(message)

    calls = [c for c in mock_cache_set.call_args_list if c[0][0] == "microstructure:token_123"]
    assert len(calls) == 1
    assert calls[0][0][1]["epoch"] == 1751500000.5
    assert calls[0][1]["ttl_seconds"] == MICROSTRUCTURE_STATE_TTL_SEC


@pytest.mark.asyncio
async def test_bump_conn_epoch_writes_redis_key(ws_service):
    """На (ре)коннекте эпоха обновляется и публикуется в Redis для читателей кэша."""
    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set:
        await ws_service._bump_conn_epoch()

    assert ws_service._conn_epoch > 0
    epoch_calls = [c for c in mock_cache_set.call_args_list if c[0][0] == "ws:conn_epoch"]
    assert len(epoch_calls) == 1
    assert epoch_calls[0][0][1] == {"epoch": ws_service._conn_epoch}


@pytest.mark.asyncio
async def test_refresh_subscriptions_subscribes_only_new_tokens(ws_service):
    """Периодический рефреш досылает subscribe только на новые токены."""
    ws_service._subscribed_markets = {"tok_old"}
    fake_ws = AsyncMock()
    ws_service._market_ws = fake_ws

    async def fake_load():
        ws_service._subscribed_markets.update({"tok_old", "tok_new"})

    with patch.object(ws_service, "_load_active_markets", side_effect=fake_load):
        await ws_service._refresh_subscriptions()

    assert fake_ws.send.call_count == 1
    import json as _json

    sent = _json.loads(fake_ws.send.call_args[0][0])
    assert sent["assets_ids"] == ["tok_new"]
    assert sent["initial_dump"] is True


@pytest.mark.asyncio
async def test_refresh_subscriptions_no_new_tokens_sends_nothing(ws_service):
    ws_service._subscribed_markets = {"tok_old"}
    fake_ws = AsyncMock()
    ws_service._market_ws = fake_ws

    with patch.object(ws_service, "_load_active_markets", AsyncMock()):
        await ws_service._refresh_subscriptions()

    fake_ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_handle_market_message_ignores_empty(ws_service):
    """Сообщение без bids/asks → не обновляет book-кэш (heartbeat всё равно пишется —
    сообщение реально пришло по WS, это и есть сигнал живости канала)."""
    message = {"market": "token_456", "type": "heartbeat"}

    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set:
        await ws_service._handle_market_message(message)

    calls = [c for c in mock_cache_set.call_args_list if c[0][0] != "ws:last_message_at"]
    assert calls == []
    mock_cache_set.assert_called_once()
    assert mock_cache_set.call_args[0][0] == "ws:last_message_at"
    assert mock_cache_set.call_args[1]["ttl_seconds"] == 600


@pytest.mark.asyncio
async def test_handle_market_message_no_token_id(ws_service):
    """Сообщение без token_id → book-кэш не обновляется (heartbeat пишется —
    сообщение реально пришло по WS)."""
    message = {"bids": [{"price": "0.55", "size": "1000"}], "asks": [{"price": "0.56", "size": "800"}]}

    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set:
        await ws_service._handle_market_message(message)

    calls = [c for c in mock_cache_set.call_args_list if c[0][0] != "ws:last_message_at"]
    assert calls == []
    mock_cache_set.assert_called_once()
    assert mock_cache_set.call_args[0][0] == "ws:last_message_at"


# === _handle_user_message ===


@pytest.mark.asyncio
async def test_handle_user_message_whale_trade(ws_service):
    """Сделка >= $5,000 → enqueue в Redis в нормализованном формате."""
    message = {
        "type": "trade",
        "size": "10000",
        "price": "0.55",
        "owner": "0xWHALE123",
        "asset_id": "token_abc",
        "side": "BUY",
    }

    mock_enqueue = AsyncMock(return_value=True)

    with patch("app.core.task_queue.enqueue", mock_enqueue):
        await ws_service._handle_user_message(message)

    # $10000 * 0.55 = $5500 >= $5000 → должен enqueue
    mock_enqueue.assert_called_once()
    call_args = mock_enqueue.call_args
    assert call_args[0][0] == "oracle:queue:transactions"
    data = call_args[0][1]
    assert data["from_address"] == "0xwhale123"
    assert data["direction"] == "buy"
    assert data["is_whale"] is True
    assert data["price"] == "0.55"
    assert data["source"] == "websocket"


@pytest.mark.asyncio
async def test_handle_user_message_small_trade(ws_service):
    """Сделка < $5,000 → не enqueue."""
    message = {
        "type": "trade",
        "size": "100",
        "price": "0.55",
        "owner": "0xSmallTrader",
    }

    mock_enqueue = AsyncMock(return_value=True)

    with patch("app.core.task_queue.enqueue", mock_enqueue):
        await ws_service._handle_user_message(message)

    # $100 * 0.55 = $55 < $5000 → не enqueue
    mock_enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_handle_user_message_ignores_non_trade(ws_service):
    """Сообщение с type != trade → игнорируется."""
    message = {"type": "heartbeat", "size": "1000000", "price": "1.0", "owner": "0xWhale"}

    await ws_service._handle_user_message(message)
    # Не crash — ок


# === reconnect ===


@pytest.mark.asyncio
async def test_reconnect_exponential_backoff(ws_service):
    """Задержки растут экспоненциально до max, после 5 failures — cooldown 300s."""
    ws_service._running = True
    delays = []

    async def mock_sleep(delay):
        delays.append(delay)

    with patch("asyncio.sleep", side_effect=mock_sleep):
        # Симулируем 5 reconnect-ов (4 exp backoff + 5-й cooldown)
        for _ in range(5):
            await ws_service._reconnect("market")

    # 4 exponential + 1 cooldown (300s)
    assert delays[0] == 1.0  # 1 * 2^0
    assert delays[1] == 2.0  # 1 * 2^1
    assert delays[2] == 4.0  # 1 * 2^2
    assert delays[3] == 8.0  # 1 * 2^3
    assert delays[4] == 300.0  # cooldown
    assert len(delays) == 5


@pytest.mark.asyncio
async def test_reconnect_max_failures_resets_and_cooldown(ws_service):
    """После 5 подряд failures → cooldown 300s, счётчик сбрасывается, канал НЕ отключается."""
    ws_service._running = True

    with patch("asyncio.sleep", new_callable=AsyncMock):
        for _ in range(5):
            await ws_service._reconnect("market")

        # Счётчик сброшен после cooldown
        assert ws_service._consecutive_failures["market"] == 0


# === subscribe / lifecycle ===


@pytest.mark.asyncio
async def test_subscribe_market_adds_to_set(ws_service):
    """subscribe_market добавляет token_id в множество подписок."""
    await ws_service.subscribe_market("token_abc")
    assert "token_abc" in ws_service._subscribed_markets


@pytest.mark.asyncio
async def test_subscribe_market_stores_mapping(ws_service):
    """subscribe_market с market_id сохраняет маппинг token_id → market_id."""
    market_uuid = "uuid-market-123"
    await ws_service.subscribe_market("token_abc", market_id=market_uuid)
    assert "token_abc" in ws_service._subscribed_markets
    assert ws_service._token_to_market_id["token_abc"] == market_uuid


@pytest.mark.asyncio
async def test_subscribe_market_without_mapping(ws_service):
    """subscribe_market без market_id не создаёт маппинг."""
    await ws_service.subscribe_market("token_abc")
    assert "token_abc" not in ws_service._token_to_market_id


@pytest.mark.asyncio
async def test_unsubscribe_market_removes_from_set(ws_service):
    """unsubscribe_market удаляет token_id из множества и маппинг."""
    ws_service._subscribed_markets.add("token_xyz")
    ws_service._token_to_market_id["token_xyz"] = "uuid-123"
    await ws_service.unsubscribe_market("token_xyz")
    assert "token_xyz" not in ws_service._subscribed_markets
    assert "token_xyz" not in ws_service._token_to_market_id


# === get_market_microstructure (public API) ===


@pytest.mark.asyncio
async def test_get_market_microstructure_from_cache():
    """Получение микроструктуры из Redis."""
    cached_data = {
        "depth_yes": 1500.0,
        "depth_no": 1100.0,
        "spread": 0.0178,
        "best_bid": 0.55,
        "best_ask": 0.56,
        "updated_at": "2025-01-01T00:00:00Z",
    }

    with patch("app.services.polymarket_ws_service.cache_get", new_callable=AsyncMock, return_value=cached_data):
        result = await get_market_microstructure("token_123")

    assert result == cached_data
    assert result["depth_yes"] == 1500.0


@pytest.mark.asyncio
async def test_get_market_microstructure_cache_miss():
    """Нет данных в кэше → None."""
    with patch("app.services.polymarket_ws_service.cache_get", new_callable=AsyncMock, return_value=None):
        result = await get_market_microstructure("unknown_token")

    assert result is None


# === _parse_orders ===


def test_parse_orders_dict_format(ws_service):
    """Парсинг ордеров из dict формата."""
    orders = [{"price": "0.55", "size": "1000"}, {"price": "0.54", "size": "500"}]
    result = ws_service._parse_orders(orders)
    assert len(result) == 2
    assert result[0] == (0.55, 1000.0)


def test_parse_orders_list_format(ws_service):
    """Парсинг ордеров из list формата."""
    orders = [[0.55, 1000], [0.54, 500]]
    result = ws_service._parse_orders(orders)
    assert len(result) == 2
    assert result[0] == (0.55, 1000.0)


def test_parse_orders_invalid_skipped(ws_service):
    """Невалидные ордера пропускаются."""
    orders = [{"price": "invalid", "size": "100"}, {"price": "0.55", "size": "1000"}, "bad_data"]
    result = ws_service._parse_orders(orders)
    assert len(result) == 1
    assert result[0] == (0.55, 1000.0)


def test_parse_orders_zero_price_skipped(ws_service):
    """Ордера с price=0 или size=0 пропускаются."""
    orders = [{"price": "0", "size": "1000"}, {"price": "0.55", "size": "0"}, {"price": "0.55", "size": "100"}]
    result = ws_service._parse_orders(orders)
    assert len(result) == 1


# === _calculate_microstructure — levels, health, midpoint ===


def test_calculate_microstructure_includes_levels(ws_service):
    """_calculate_microstructure возвращает top-10 уровней bids/asks."""
    bids = [{"price": str(0.60 - i * 0.01), "size": str(1000 + i * 100)} for i in range(15)]
    asks = [{"price": str(0.61 + i * 0.01), "size": str(800 + i * 50)} for i in range(15)]

    result = ws_service._calculate_microstructure(bids, asks)

    assert "levels" in result
    assert len(result["levels"]["bids"]) == 10
    assert len(result["levels"]["asks"]) == 10
    # Bids sorted desc by price
    assert result["levels"]["bids"][0]["price"] == 0.60
    assert result["levels"]["bids"][9]["price"] == 0.51
    # Asks sorted asc by price
    assert result["levels"]["asks"][0]["price"] == 0.61
    assert result["levels"]["asks"][9]["price"] == 0.70


def test_calculate_microstructure_levels_fewer_than_10(ws_service):
    """Если уровней < 10, возвращать все что есть."""
    bids = [{"price": "0.55", "size": "1000"}, {"price": "0.54", "size": "500"}]
    asks = [{"price": "0.56", "size": "800"}]

    result = ws_service._calculate_microstructure(bids, asks)

    assert len(result["levels"]["bids"]) == 2
    assert len(result["levels"]["asks"]) == 1


def test_calculate_microstructure_health_healthy(ws_service):
    """Узкий spread < 0.02 → health = healthy."""
    bids = [{"price": "0.559", "size": "10000"}]
    asks = [{"price": "0.560", "size": "10000"}]

    result = ws_service._calculate_microstructure(bids, asks)
    assert result["health"] == "healthy"


def test_calculate_microstructure_health_moderate(ws_service):
    """Spread 0.02-0.05 → health = moderate."""
    # spread = (0.58 - 0.56) / 0.58 ≈ 0.0345 — в диапазоне [0.02, 0.05)
    bids = [{"price": "0.56", "size": "1000"}]
    asks = [{"price": "0.58", "size": "1000"}]

    result = ws_service._calculate_microstructure(bids, asks)
    assert result["health"] == "moderate"


def test_calculate_microstructure_health_illiquid(ws_service):
    """Spread > 0.05 → health = illiquid."""
    bids = [{"price": "0.40", "size": "100"}]
    asks = [{"price": "0.60", "size": "100"}]

    result = ws_service._calculate_microstructure(bids, asks)
    assert result["health"] == "illiquid"


def test_calculate_microstructure_midpoint(ws_service):
    """midpoint = (best_bid + best_ask) / 2."""
    bids = [{"price": "0.55", "size": "1000"}]
    asks = [{"price": "0.65", "size": "1000"}]

    result = ws_service._calculate_microstructure(bids, asks)
    assert result["midpoint"] == pytest.approx(0.60)


# === ringbuffer hook (whale-shock backward window) ===


@pytest.mark.asyncio
async def test_handle_market_message_pushes_to_ringbuffer(ws_service):
    """На book-update WSS вызывает ring_push с token_id и ts_unix.

    ring_push импортируется локально в _handle_market_message — мокаем по месту
    определения (app.services.snapshot_recorder.ring_push).
    """
    message = {
        "market": "tok_push_1",
        "type": "book",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "100"}],
    }

    with (
        patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock),
        patch("app.services.snapshot_recorder.ring_push", new_callable=AsyncMock) as mock_push,
    ):
        await ws_service._handle_market_message(message)

    mock_push.assert_awaited_once()
    assert mock_push.await_args.args[0] == "tok_push_1"
    # ts_unix передаётся kwarg-ом
    assert "ts_unix" in mock_push.await_args.kwargs


# === id-mismatch regression (whale-shock recovery=0 root cause, 2026-06-08) ===
# Реальное Polymarket book-message несёт ОБА поля: asset_id (CLOB token_id, decimal)
# и market (condition_id, hex). Recorder читает ring/microstructure по token_id из
# markets.clob_token_ids (= asset_id-пространство). Поэтому WSS обязан индексировать
# по asset_id, а НЕ по market — иначе ключи не совпадают и backward/forward пусты.
# См. docs.polymarket.com/developers/CLOB/websocket/market-channel.

_REAL_ASSET_ID = "65818619657568813474341868652308942079804919287380422192892211131408793125422"
_REAL_CONDITION_ID = "0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af"


@pytest.mark.asyncio
async def test_handle_market_message_indexes_by_asset_id_not_condition_id(ws_service):
    """book-message с asset_id+market → cache/ring индексируются по asset_id (token_id).

    Regression: код брал message['market'] (condition_id) первым → ring/microstructure
    индексировались по condition_id, а record_event читает по token_id → нестыковка,
    mv_whale_shock_recovery=0.
    """
    message = {
        "event_type": "book",
        "asset_id": _REAL_ASSET_ID,
        "market": _REAL_CONDITION_ID,
        "bids": [{"price": "0.55", "size": "1000"}],
        "asks": [{"price": "0.56", "size": "800"}],
    }

    with (
        patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set,
        patch("app.services.snapshot_recorder.ring_push", new_callable=AsyncMock) as mock_push,
    ):
        await ws_service._handle_market_message(message)

    # ring_push должен получить token_id (asset_id), НЕ condition_id.
    mock_push.assert_awaited_once()
    assert mock_push.await_args.args[0] == _REAL_ASSET_ID, "ring_push must index by asset_id (token_id), not condition_id"

    # cache_set ключи microstructure/orderbook — по asset_id.
    cache_keys = [call[0][0] for call in mock_cache_set.call_args_list]
    assert f"microstructure:{_REAL_ASSET_ID}" in cache_keys
    assert f"orderbook:{_REAL_ASSET_ID}" in cache_keys
    # condition_id НЕ должен использоваться как token-ключ.
    assert f"microstructure:{_REAL_CONDITION_ID}" not in cache_keys
    assert f"ring:{_REAL_CONDITION_ID}" not in cache_keys


@pytest.mark.asyncio
async def test_handle_best_bid_ask_indexes_by_asset_id_not_condition_id(ws_service):
    """best_bid_ask с asset_id+market → microstructure-cache по asset_id (token_id)."""
    message = {
        "event_type": "best_bid_ask",
        "asset_id": _REAL_ASSET_ID,
        "market": _REAL_CONDITION_ID,
        "best_bid": "0.55",
        "best_ask": "0.56",
    }

    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set:
        await ws_service._handle_best_bid_ask(message)

    cache_keys = [call[0][0] for call in mock_cache_set.call_args_list]
    assert f"microstructure:{_REAL_ASSET_ID}" in cache_keys, "best_bid_ask must index by asset_id, not condition_id"
    assert f"microstructure:{_REAL_CONDITION_ID}" not in cache_keys


# === Write-through: _dirty_prices / _flush_prices_loop ===


@pytest.mark.asyncio
async def test_handle_market_message_marks_dirty_price_for_yes_token(ws_service):
    """book-message на YES-токен рынка → midpoint попадает в _dirty_prices."""
    ws_service._yes_token_markets[_REAL_ASSET_ID] = "market-uuid-1"
    message = {
        "event_type": "book",
        "asset_id": _REAL_ASSET_ID,
        "market": _REAL_CONDITION_ID,
        "bids": [{"price": "0.55", "size": "1000"}],
        "asks": [{"price": "0.57", "size": "800"}],
    }

    with (
        patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock),
        patch("app.services.snapshot_recorder.ring_push", new_callable=AsyncMock),
    ):
        await ws_service._handle_market_message(message)

    assert "market-uuid-1" in ws_service._dirty_prices
    assert ws_service._dirty_prices["market-uuid-1"] == pytest.approx(0.56)


@pytest.mark.asyncio
async def test_handle_market_message_ignores_non_yes_token(ws_service):
    """token_id не в _yes_token_markets (NO-нога или неотслеживаемый рынок) → dirty-set не трогается."""
    message = {
        "event_type": "book",
        "asset_id": _REAL_ASSET_ID,
        "market": _REAL_CONDITION_ID,
        "bids": [{"price": "0.55", "size": "1000"}],
        "asks": [{"price": "0.57", "size": "800"}],
    }

    with (
        patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock),
        patch("app.services.snapshot_recorder.ring_push", new_callable=AsyncMock),
    ):
        await ws_service._handle_market_message(message)

    assert ws_service._dirty_prices == {}


@pytest.mark.asyncio
async def test_handle_best_bid_ask_marks_dirty_price_for_yes_token(ws_service):
    """best_bid_ask на YES-токен → midpoint в _dirty_prices."""
    ws_service._yes_token_markets[_REAL_ASSET_ID] = "market-uuid-2"
    message = {
        "event_type": "best_bid_ask",
        "asset_id": _REAL_ASSET_ID,
        "market": _REAL_CONDITION_ID,
        "best_bid": "0.40",
        "best_ask": "0.42",
    }

    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock):
        await ws_service._handle_best_bid_ask(message)

    assert ws_service._dirty_prices["market-uuid-2"] == pytest.approx(0.41)


def test_mark_dirty_price_ignores_garbage_midpoint(ws_service):
    ws_service._yes_token_markets["tokX"] = "market-uuid-3"
    ws_service._mark_dirty_price("tokX", "not-a-number")
    assert ws_service._dirty_prices == {}


def test_mark_dirty_price_ignores_out_of_range_midpoint(ws_service):
    ws_service._yes_token_markets["tokX"] = "market-uuid-3"
    ws_service._mark_dirty_price("tokX", 1.5)
    assert ws_service._dirty_prices == {}


@pytest.mark.asyncio
async def test_flush_prices_loop_batches_and_clears_dirty_set(ws_service):
    """_flush_prices_loop: один проход после sleep → executemany с батчем, dirty-set очищается."""
    ws_service._running = True
    ws_service._dirty_prices = {"m1": 0.42, "m2": 0.61}

    settings = MagicMock()
    settings.ws_price_write_through_enabled = True

    async def _fake_sleep(_seconds):
        # После первой итерации останавливаем цикл, чтобы тест не завис.
        ws_service._running = False

    with (
        patch("app.services.polymarket_ws_service.get_settings", return_value=settings),
        patch("app.services.polymarket_ws_service.asyncio.sleep", _fake_sleep),
        patch("app.services.database.executemany", new_callable=AsyncMock) as mock_executemany,
    ):
        await ws_service._flush_prices_loop()

    mock_executemany.assert_awaited_once()
    query, rows = mock_executemany.call_args.args
    assert "UPDATE markets" in query
    assert "current_price" in query
    assert set(rows) == {("m1", 0.42), ("m2", 0.61)}
    assert ws_service._dirty_prices == {}


@pytest.mark.asyncio
async def test_flush_prices_loop_skips_when_disabled(ws_service):
    """ws_price_write_through_enabled=False → executemany не вызывается, dirty-set не трогается."""
    ws_service._running = True
    ws_service._dirty_prices = {"m1": 0.42}

    settings = MagicMock()
    settings.ws_price_write_through_enabled = False

    async def _fake_sleep(_seconds):
        ws_service._running = False

    with (
        patch("app.services.polymarket_ws_service.get_settings", return_value=settings),
        patch("app.services.polymarket_ws_service.asyncio.sleep", _fake_sleep),
        patch("app.services.database.executemany", new_callable=AsyncMock) as mock_executemany,
    ):
        await ws_service._flush_prices_loop()

    mock_executemany.assert_not_awaited()
    assert ws_service._dirty_prices == {"m1": 0.42}


@pytest.mark.asyncio
async def test_flush_prices_loop_skips_when_dirty_set_empty(ws_service):
    """Пустой dirty-set → executemany не вызывается вообще."""
    ws_service._running = True
    ws_service._dirty_prices = {}

    settings = MagicMock()
    settings.ws_price_write_through_enabled = True

    async def _fake_sleep(_seconds):
        ws_service._running = False

    with (
        patch("app.services.polymarket_ws_service.get_settings", return_value=settings),
        patch("app.services.polymarket_ws_service.asyncio.sleep", _fake_sleep),
        patch("app.services.database.executemany", new_callable=AsyncMock) as mock_executemany,
    ):
        await ws_service._flush_prices_loop()

    mock_executemany.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_active_markets_populates_yes_token_markets(ws_service):
    """_load_active_markets заполняет _yes_token_markets из yes_token (clob_token_ids->>0)."""
    rows = [
        {"id": "market-1", "token_id": "yes_tok_1", "yes_token": "yes_tok_1"},
        {"id": "market-1", "token_id": "no_tok_1", "yes_token": "yes_tok_1"},
        {"id": "market-2", "token_id": "yes_tok_2", "yes_token": "yes_tok_2"},
    ]

    with patch("app.services.database.fetch", new_callable=AsyncMock, return_value=rows):
        await ws_service._load_active_markets()

    assert ws_service._yes_token_markets == {"yes_tok_1": "market-1", "yes_tok_2": "market-2"}
    assert ws_service._subscribed_markets == {"yes_tok_1", "no_tok_1", "yes_tok_2"}


@pytest.mark.asyncio
async def test_load_active_markets_limits_markets_not_tokens(ws_service):
    """LIMIT 5000 должен резать РЫНКИ, а не строки market×token после
    jsonb_array_elements_text (latency-аудит 2026-07-06: LIMIT после разворота
    токенов давал top-2500 рынков вместо top-5000)."""
    with patch("app.services.database.fetch", new_callable=AsyncMock, return_value=[]) as mock_fetch:
        await ws_service._load_active_markets()

    sql = mock_fetch.call_args[0][0]
    assert sql.index("LIMIT 5000") < sql.index("jsonb_array_elements_text")


# === Heartbeat freshness (audit second-tier: WS ping/pong + heartbeat) ===


@pytest.mark.asyncio
async def test_record_heartbeat_writes_ws_last_message_at(ws_service):
    """_record_heartbeat пишет freshness-метку с ttl 600s."""
    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set:
        await ws_service._record_heartbeat()

    mock_cache_set.assert_called_once()
    key, value = mock_cache_set.call_args[0]
    assert key == "ws:last_message_at"
    assert "ts" in value
    assert mock_cache_set.call_args[1]["ttl_seconds"] == 600


@pytest.mark.asyncio
async def test_record_heartbeat_throttled_within_5s(ws_service):
    """Повторный вызов в течение 5с после предыдущего — no-op (не долбим Redis)."""
    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set:
        await ws_service._record_heartbeat()
        await ws_service._record_heartbeat()
        await ws_service._record_heartbeat()

    mock_cache_set.assert_called_once()


@pytest.mark.asyncio
async def test_record_heartbeat_fires_again_after_throttle_window(ws_service):
    """Прошло >= 5с с последней записи → heartbeat пишется снова."""
    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set:
        await ws_service._record_heartbeat()
        ws_service._last_hb_ts -= 6.0  # имитируем прошедшее время
        await ws_service._record_heartbeat()

    assert mock_cache_set.call_count == 2


@pytest.mark.asyncio
async def test_handle_market_message_calls_record_heartbeat(ws_service):
    """_handle_market_message всегда триггерит heartbeat-попытку, независимо от формы сообщения."""
    with patch.object(ws_service, "_record_heartbeat", new_callable=AsyncMock) as mock_hb:
        await ws_service._handle_market_message({"market": "tok", "type": "unknown"})

    mock_hb.assert_awaited_once()


def test_ws_ping_interval_and_timeout_constants_restored():
    """Протокольный ping/pong (RFC 6455) должен быть включён на обоих каналах —
    audit second-tier: раньше ping_interval/ping_timeout=None полагались только
    на текстовый PING-механизм приложения Polymarket, который не ловит half-open
    TCP (сервер не отвечает, но соединение формально жив-о). websockets.connect(...)
    должен звать с ping_interval=30, ping_timeout=10 в обоих местах — market и user."""
    import inspect

    from app.services import polymarket_ws_service as mod

    market_src = inspect.getsource(mod.PolymarketWebSocketService.connect_market_channel)
    user_src = inspect.getsource(mod.PolymarketWebSocketService.connect_user_channel)

    assert "ping_interval=30" in market_src
    assert "ping_timeout=10" in market_src
    assert "ping_interval=30" in user_src
    assert "ping_timeout=10" in user_src
    # Текстовый PING-механизм должен остаться (для приложения Polymarket)
    assert "_ping_loop" in market_src
    assert "_ping_loop" in user_src

# === REST /books bootstrap (Fix-0, latency-аудит 2026-07-06) ===


@pytest.mark.asyncio
async def test_seed_books_via_rest_writes_rest_seed_snapshots(ws_service):
    """Книги из POST /books пишутся в microstructure:{token} (+ {market_id}
    при маппинге) как rest_seed без epoch — та же семантика, что seed_book_snapshot."""
    from app.services.live_prices import REST_SEED_TTL_SEC

    await ws_service.subscribe_market("tok_yes", market_id="mkt-1")
    books = [
        {
            "asset_id": "tok_yes",
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.46", "size": "50"}],
        }
    ]
    mock_poller = MagicMock()
    mock_poller.fetch_order_books_rest = AsyncMock(return_value=books)

    with (
        patch("app.services.polymarket_poller_service.get_poller_service", return_value=mock_poller),
        patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set,
    ):
        seeded = await ws_service._seed_books_via_rest(["tok_yes"])

    assert seeded == 1
    written = {c[0][0]: (c[0][1], c[1]["ttl_seconds"]) for c in mock_cache_set.call_args_list}
    micro, ttl = written["microstructure:tok_yes"]
    assert micro["source"] == "rest_seed"
    assert "epoch" not in micro
    assert ttl == REST_SEED_TTL_SEC
    assert "microstructure:mkt-1" in written


@pytest.mark.asyncio
async def test_seed_books_via_rest_chunks_requests(ws_service):
    """Токены режутся чанками по 100 на POST — бюджет 500 req/10с не выбивается."""
    tokens = [f"t{i}" for i in range(250)]
    mock_poller = MagicMock()
    mock_poller.fetch_order_books_rest = AsyncMock(return_value=[])

    with (
        patch("app.services.polymarket_poller_service.get_poller_service", return_value=mock_poller),
        patch("app.services.polymarket_ws_service.asyncio.sleep", new_callable=AsyncMock),
    ):
        await ws_service._seed_books_via_rest(tokens)

    assert mock_poller.fetch_order_books_rest.await_count == 3
    sizes = [len(c.args[0]) for c in mock_poller.fetch_order_books_rest.await_args_list]
    assert sizes == [100, 100, 50]


@pytest.mark.asyncio
async def test_seed_books_via_rest_skips_freshly_ticked(ws_service):
    """Токен, тикнувший по WS во время посева, не фетчится и не перетирается."""
    ws_service._ticked_tokens.add("tok_yes")
    mock_poller = MagicMock()
    mock_poller.fetch_order_books_rest = AsyncMock(return_value=[])

    with (
        patch("app.services.polymarket_poller_service.get_poller_service", return_value=mock_poller),
        patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock) as mock_cache_set,
    ):
        seeded = await ws_service._seed_books_via_rest(["tok_yes"])

    assert seeded == 0
    mock_poller.fetch_order_books_rest.assert_not_awaited()
    mock_cache_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_market_message_tracks_ticked_tokens(ws_service):
    """Book-сообщение помечает токен тикнувшим — периодический re-seed его пропустит."""
    message = {
        "market": "token_123",
        "bids": [{"price": "0.55", "size": "1000"}],
        "asks": [{"price": "0.56", "size": "800"}],
    }
    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock):
        await ws_service._handle_market_message(message)

    assert "token_123" in ws_service._ticked_tokens


@pytest.mark.asyncio
async def test_bump_conn_epoch_clears_ticked_tokens(ws_service):
    """Новый коннект = новые наблюдения: тики прошлого соединения не должны
    заставлять bootstrap пропускать токены."""
    ws_service._ticked_tokens.add("stale_tok")
    with patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock):
        await ws_service._bump_conn_epoch()

    assert ws_service._ticked_tokens == set()


@pytest.mark.asyncio
async def test_seed_all_books_seeds_all_yes_tokens(ws_service):
    """Bootstrap после (ре)коннекта сеет все YES-токены подписки."""
    ws_service._yes_token_markets = {"y2": "m2", "y1": "m1"}
    with patch.object(ws_service, "_seed_books_via_rest", new_callable=AsyncMock) as mock_seed:
        await ws_service._seed_all_books()

    mock_seed.assert_awaited_once_with(["y1", "y2"])


@pytest.mark.asyncio
async def test_reseed_quiet_books_seeds_only_quiet_yes_tokens(ws_service):
    """Периодический re-seed берёт только YES-токены без тиков с прошлого цикла
    и сбрасывает ticked-набор."""
    ws_service._yes_token_markets = {"y1": "m1", "y2": "m2"}
    ws_service._ticked_tokens = {"y1", "n1"}
    with patch.object(ws_service, "_seed_books_via_rest", new_callable=AsyncMock) as mock_seed:
        await ws_service._reseed_quiet_books()

    mock_seed.assert_awaited_once_with(["y2"])
    assert ws_service._ticked_tokens == set()


@pytest.mark.asyncio
async def test_best_bid_ask_tracks_ticked_tokens(ws_service):
    """Partial-апдейт (best_bid_ask) — тоже живой тик: re-seed его не перетирает."""
    message = {
        "type": "best_bid_ask",
        "asset_id": "token_123",
        "best_bid": "0.55",
        "best_ask": "0.56",
    }
    with (
        patch("app.services.polymarket_ws_service.cache_get", AsyncMock(return_value=None)),
        patch("app.services.polymarket_ws_service.cache_set", new_callable=AsyncMock),
    ):
        await ws_service._handle_best_bid_ask(message)

    assert "token_123" in ws_service._ticked_tokens
