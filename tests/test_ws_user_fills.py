"""
Тесты для fills собственных ордеров из user-канала WSS → execution_orders.

Формат событий user-канала Polymarket в бою неизвестен на момент написания —
extract_fill_candidates тестируется на НАШЕЙ функции маппинга (толерантной
к вариациям формата), не на угаданном формате Polymarket. Первый прод-fill
покажет реальные ключи через лог ws_user_event_raw; extract_fill_candidates
поправить при расхождении.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.polymarket_ws_service import PolymarketWebSocketService, extract_fill_candidates

# === extract_fill_candidates (модульная функция, без инстанса сервиса) ===


def test_extract_fill_candidates_taker():
    msg = {
        "type": "order_matched",
        "taker_order_id": "0xabc",
        "asset_id": "tok1",
        "price": "0.52",
        "size": "10",
    }
    fills = extract_fill_candidates(msg)
    assert fills == [{"order_id": "0xabc", "price": 0.52, "size": 10.0}]


def test_extract_fill_candidates_maker_list():
    msg = {
        "type": "trade",
        "maker_orders": [{"order_id": "0xdef", "price": "0.40", "matched_amount": "5"}],
        "asset_id": "tok1",
        "price": "0.40",
        "size": "5",
    }
    fills = extract_fill_candidates(msg)
    assert {"order_id": "0xdef", "price": 0.40, "size": 5.0} in fills


def test_extract_ignores_unknown_shapes():
    assert extract_fill_candidates({"type": "order_matched"}) == []


def test_extract_fill_candidates_camel_case_taker():
    """takerOrderId camelCase-вариант (Polymarket иногда шлёт camelCase)."""
    msg = {"type": "order_matched", "takerOrderId": "0xcam", "price": "0.30", "amount": "2"}
    fills = extract_fill_candidates(msg)
    assert fills == [{"order_id": "0xcam", "price": 0.30, "size": 2.0}]


def test_extract_fill_candidates_zero_or_missing_price_dropped():
    """price=0 или отсутствует size → кандидат не формируется (не делить на ноль дальше по пайплайну)."""
    msg = {"type": "order_matched", "taker_order_id": "0xzero", "price": "0", "size": "10"}
    assert extract_fill_candidates(msg) == []


def test_extract_fill_candidates_maker_missing_order_id_skipped():
    msg = {
        "type": "trade",
        "maker_orders": [{"price": "0.40", "matched_amount": "5"}],  # без order_id
        "price": "0.40",
        "size": "5",
    }
    assert extract_fill_candidates(msg) == []


# === _handle_user_message: интеграция с execution_orders ===


@pytest.fixture
def ws_service():
    with patch("app.services.polymarket_ws_service.get_settings"):
        return PolymarketWebSocketService()


@pytest.mark.asyncio
async def test_handle_user_message_applies_fill(ws_service):
    """order_matched с fill-кандидатом → UPDATE execution_orders SET status='filled'."""
    message = {
        "type": "order_matched",
        "taker_order_id": "0xabc",
        "asset_id": "tok1",
        "price": "0.52",
        "size": "10",
    }

    with patch("app.services.database.execute", new_callable=AsyncMock) as mock_execute:
        mock_execute.return_value = "UPDATE 1"
        await ws_service._handle_user_message(message)

    mock_execute.assert_awaited_once()
    args, _ = mock_execute.call_args
    sql = args[0]
    assert "UPDATE execution_orders" in sql
    assert "status = 'filled'" in sql
    assert args[1] == ["0xabc"]
    assert args[2] == pytest.approx(0.52)


@pytest.mark.asyncio
async def test_handle_user_message_no_fill_candidates_skips_db(ws_service):
    """Событие без опознанных полей ордера (например только whale-детекция) — execute не вызывается."""
    message = {"type": "order_matched", "price": "0.10", "size": "5000"}

    with patch("app.services.database.execute", new_callable=AsyncMock) as mock_execute:
        await ws_service._handle_user_message(message)

    mock_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_user_message_ignores_non_trade_types(ws_service):
    """Не trade/order_matched типы — extract_fill_candidates даже не должен звать execute."""
    message = {"type": "book", "taker_order_id": "0xabc", "price": "0.5", "size": "10"}

    with patch("app.services.database.execute", new_callable=AsyncMock) as mock_execute:
        await ws_service._handle_user_message(message)

    mock_execute.assert_not_awaited()
