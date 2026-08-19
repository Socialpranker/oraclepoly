"""Tests for app.services.live_exit — SELL FOK выход из live-позиции (audit топ-1).

Live-путь был BUY-only: SELL не существовал нигде. close_live_position — сервис
аварийного/автоматического (TP/SL/max_hold) закрытия filled live-позиции.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        database_url="postgresql://x:x@localhost/x",
        openai_api_key="sk-x",
        redis_url="redis://localhost",
        telegram_bot_token="1:x",
        admin_telegram_ids=[1],
        admin_api_key="x",
        polymarket_api_key="x",
        polymarket_api_secret="y",
        polymarket_api_passphrase="z",
    )
    defaults.update(kwargs)
    return Settings(**defaults)


class _RecordingClient:
    """Фейковый CLOB-клиент: пишет, какой order-метод и с чем был вызван."""

    def __init__(self, resp: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._resp = resp if resp is not None else {"success": True, "orderID": "sell-oid-1"}

    def set_api_creds(self, _creds):  # noqa: D401
        pass

    def create_and_post_market_order(self, **kwargs):
        self.calls.append(("create_and_post_market_order", kwargs))
        return self._resp


@pytest.fixture
def patched_live_exit(monkeypatch):
    """Подменяет PolymarketService.clob_client, settings и fetch_live_book."""
    import app.services.live_exit as live_exit_mod

    rec = _RecordingClient()

    class _FakeSvc:
        clob_client = rec

    monkeypatch.setattr(live_exit_mod, "get_settings", lambda: _make_settings())
    monkeypatch.setattr(
        "app.services.polymarket_service.PolymarketService", lambda: _FakeSvc()
    )
    return live_exit_mod, rec


FILLED_ROW = {
    "id": "order-db-1",
    "status": "filled",
    "stake": 100.0,
    "fill_price": 0.40,
    "filled_qty": 250.0,
    "market_id": "0xmarket",
    "token_id": "tok-abc",
}


def _reservation_row(order_id: str = "order-db-1"):
    """Строка, которую возвращает fetchrow(...RETURNING id) успешной резервации."""
    return {"id": order_id}


@pytest.mark.asyncio
async def test_close_live_position_happy_path(monkeypatch, patched_live_exit):
    """filled позиция + живой book → SELL размещён, status='exiting'."""
    live_exit_mod, rec = patched_live_exit

    fetchrow_calls: list = []

    async def mock_fetchrow(_sql, *args):
        fetchrow_calls.append(args)
        if len(fetchrow_calls) == 1:
            return dict(FILLED_ROW)
        return _reservation_row()

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)
        return "UPDATE 1"

    async def mock_fetch_live_book(_token_id):
        return {"bid": 0.42, "ask": 0.44, "bid_size": 500.0, "ask_size": 500.0}

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(live_exit_mod, "execute", mock_execute)
    monkeypatch.setattr("app.services.execution_safety.fetch_live_book", mock_fetch_live_book)

    ok, msg = await live_exit_mod.close_live_position("order-db-1", reason="manual_tg")

    assert ok is True
    assert msg == "sell-oid-1"
    assert len(rec.calls) == 1
    method, kwargs = rec.calls[0]
    assert method == "create_and_post_market_order"
    order_args = kwargs["order_args"]
    assert order_args.side == "SELL"
    assert order_args.token_id == "tok-abc"
    # shares = filled_qty (уже известно из fill) — 250 shares.
    assert order_args.amount == pytest.approx(250.0)
    # floor = bid * (1 - 0.02) = 0.42 * 0.98 = 0.4116 → round 2dp = 0.41
    assert order_args.price == pytest.approx(0.41)

    # UPDATE вызван один раз — exit_pending -> exiting (после успешного CLOB-похода).
    assert len(captured_updates) == 1
    db_id, exit_order_id = captured_updates[0]
    assert db_id == "order-db-1"
    assert exit_order_id == "sell-oid-1"


@pytest.mark.asyncio
async def test_close_live_position_reserves_exit_pending_before_clob_call(monkeypatch, patched_live_exit):
    """Резервация exit_pending (fetchrow RETURNING) должна произойти ДО похода на CLOB —
    порядок операций critical для устранения гонки double-close."""
    live_exit_mod, rec = patched_live_exit

    call_order: list[str] = []

    async def mock_fetchrow(_sql, *args):
        if len(call_order) == 0 or call_order[-1] != "reserve":
            call_order.append("select")
            return dict(FILLED_ROW)
        return _reservation_row()

    async def mock_fetchrow_tracking(_sql, *args):
        result = await mock_fetchrow(_sql, *args)
        if "RETURNING" in _sql:
            call_order.append("reserve")
        return result

    async def mock_execute(_sql, *args):
        call_order.append("mark_exiting")
        return "UPDATE 1"

    async def mock_fetch_live_book(_token_id):
        call_order.append("book_lookup")
        return {"bid": 0.42, "ask": 0.44, "bid_size": 500.0, "ask_size": 500.0}

    original_create = rec.create_and_post_market_order

    def tracking_create(**kwargs):
        call_order.append("clob_call")
        return original_create(**kwargs)

    rec.create_and_post_market_order = tracking_create

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow_tracking)
    monkeypatch.setattr(live_exit_mod, "execute", mock_execute)
    monkeypatch.setattr("app.services.execution_safety.fetch_live_book", mock_fetch_live_book)

    ok, _msg = await live_exit_mod.close_live_position("order-db-1", reason="manual_tg")

    assert ok is True
    reserve_idx = call_order.index("reserve")
    clob_idx = call_order.index("clob_call")
    assert reserve_idx < clob_idx, f"reserve должен случиться до clob_call, порядок был: {call_order}"


@pytest.mark.asyncio
async def test_close_live_position_race_second_caller_gets_already_exiting(monkeypatch, patched_live_exit):
    """Второй одновременный вызов после успешного резерва первого получает
    already_exiting и НЕ идёт на CLOB (устраняет двойной SELL)."""
    live_exit_mod, rec = patched_live_exit

    async def mock_fetchrow(_sql, *args):
        if "RETURNING" in _sql:
            return None  # резерв уже взят первым вызовом — гонка проиграна
        return dict(FILLED_ROW)

    async def mock_execute(_sql, *args):
        return "UPDATE 1"

    async def mock_fetch_live_book(_token_id):
        return {"bid": 0.42, "ask": 0.44, "bid_size": 500.0, "ask_size": 500.0}

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(live_exit_mod, "execute", mock_execute)
    monkeypatch.setattr("app.services.execution_safety.fetch_live_book", mock_fetch_live_book)

    ok, msg = await live_exit_mod.close_live_position("order-db-1", reason="manual_tg")

    assert ok is False
    assert msg == "already_exiting"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_close_live_position_clob_failure_rolls_back_to_filled(monkeypatch, patched_live_exit):
    """CLOB бросает exception после успешного резерва → откат exit_pending -> filled."""
    live_exit_mod, rec = patched_live_exit

    def raising_create(**kwargs):
        raise RuntimeError("network timeout")

    rec.create_and_post_market_order = raising_create

    async def mock_fetchrow(_sql, *args):
        if "RETURNING" in _sql:
            return _reservation_row()
        return dict(FILLED_ROW)

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append((_sql, args))
        return "UPDATE 1"

    async def mock_fetch_live_book(_token_id):
        return {"bid": 0.42, "ask": 0.44, "bid_size": 500.0, "ask_size": 500.0}

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(live_exit_mod, "execute", mock_execute)
    monkeypatch.setattr("app.services.execution_safety.fetch_live_book", mock_fetch_live_book)

    ok, msg = await live_exit_mod.close_live_position("order-db-1", reason="manual_tg")

    assert ok is False
    assert msg == "exit_post_failed"
    assert len(captured_updates) == 1
    sql, args = captured_updates[0]
    assert "'filled'" in sql or "filled" in sql
    assert args[0] == "order-db-1"


@pytest.mark.asyncio
async def test_close_live_position_clob_rejected_rolls_back_to_filled(monkeypatch, patched_live_exit):
    """CLOB отвечает success=False после успешного резерва → откат exit_pending -> filled."""
    live_exit_mod, rec = patched_live_exit
    rec._resp = {"success": False, "errorMsg": "insufficient balance"}

    async def mock_fetchrow(_sql, *args):
        if "RETURNING" in _sql:
            return _reservation_row()
        return dict(FILLED_ROW)

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append((_sql, args))
        return "UPDATE 1"

    async def mock_fetch_live_book(_token_id):
        return {"bid": 0.42, "ask": 0.44, "bid_size": 500.0, "ask_size": 500.0}

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(live_exit_mod, "execute", mock_execute)
    monkeypatch.setattr("app.services.execution_safety.fetch_live_book", mock_fetch_live_book)

    ok, msg = await live_exit_mod.close_live_position("order-db-1", reason="manual_tg")

    assert ok is False
    assert msg == "exit_post_failed"
    assert len(captured_updates) == 1
    sql, args = captured_updates[0]
    assert "filled" in sql
    assert args[0] == "order-db-1"


@pytest.mark.asyncio
async def test_close_live_position_no_order_id_rolls_back_to_filled(monkeypatch, patched_live_exit):
    """success=True, но orderID отсутствует → откат exit_pending -> filled."""
    live_exit_mod, rec = patched_live_exit
    rec._resp = {"success": True}

    async def mock_fetchrow(_sql, *args):
        if "RETURNING" in _sql:
            return _reservation_row()
        return dict(FILLED_ROW)

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append((_sql, args))
        return "UPDATE 1"

    async def mock_fetch_live_book(_token_id):
        return {"bid": 0.42, "ask": 0.44, "bid_size": 500.0, "ask_size": 500.0}

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(live_exit_mod, "execute", mock_execute)
    monkeypatch.setattr("app.services.execution_safety.fetch_live_book", mock_fetch_live_book)

    ok, msg = await live_exit_mod.close_live_position("order-db-1", reason="manual_tg")

    assert ok is False
    assert msg == "exit_post_failed"
    assert len(captured_updates) == 1


@pytest.mark.asyncio
async def test_close_live_position_falls_back_to_stake_over_fill_price(monkeypatch, patched_live_exit):
    """filled_qty отсутствует (NULL) → shares = stake / fill_price."""
    live_exit_mod, rec = patched_live_exit

    row = dict(FILLED_ROW)
    row["filled_qty"] = None  # заставляем fallback
    row["stake"] = 100.0
    row["fill_price"] = 0.40

    async def mock_fetchrow(_sql, *args):
        if "RETURNING" in _sql:
            return _reservation_row()
        return row

    async def mock_execute(_sql, *args):
        return "UPDATE 1"

    async def mock_fetch_live_book(_token_id):
        return {"bid": 0.42, "ask": 0.44, "bid_size": 500.0, "ask_size": 500.0}

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(live_exit_mod, "execute", mock_execute)
    monkeypatch.setattr("app.services.execution_safety.fetch_live_book", mock_fetch_live_book)

    ok, msg = await live_exit_mod.close_live_position("order-db-1", reason="take_profit")

    assert ok is True
    order_args = rec.calls[0][1]["order_args"]
    # 100 / 0.40 = 250 shares
    assert order_args.amount == pytest.approx(250.0)


@pytest.mark.asyncio
async def test_close_live_position_order_not_found(monkeypatch, patched_live_exit):
    live_exit_mod, rec = patched_live_exit

    async def mock_fetchrow(_sql, _order_id):
        return None

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)

    ok, msg = await live_exit_mod.close_live_position("missing-id", reason="manual_tg")

    assert ok is False
    assert msg == "order_not_found"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_close_live_position_not_filled_status(monkeypatch, patched_live_exit):
    """status != 'filled' (уже exiting/settled/placed) → отказ, без SELL."""
    live_exit_mod, rec = patched_live_exit

    row = dict(FILLED_ROW)
    row["status"] = "exiting"

    async def mock_fetchrow(_sql, _order_id):
        return row

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)

    ok, msg = await live_exit_mod.close_live_position("order-db-1", reason="manual_tg")

    assert ok is False
    assert msg == "not_closeable:exiting"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_close_live_position_no_book_returns_none(monkeypatch, patched_live_exit):
    """fetch_live_book вернул None (WS+REST оба недоступны) → отказ без SELL,
    резерв exit_pending откатывается обратно в filled (rollback)."""
    live_exit_mod, rec = patched_live_exit

    async def mock_fetchrow(_sql, *args):
        if "RETURNING" in _sql:
            return _reservation_row()
        return dict(FILLED_ROW)

    rollback_calls: list = []

    async def mock_execute(_sql, *args):
        rollback_calls.append((_sql, args))
        return "UPDATE 1"

    async def mock_fetch_live_book(_token_id):
        return None

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(live_exit_mod, "execute", mock_execute)
    monkeypatch.setattr("app.services.execution_safety.fetch_live_book", mock_fetch_live_book)

    ok, msg = await live_exit_mod.close_live_position("order-db-1", reason="manual_tg")

    assert ok is False
    assert msg == "no_book_for_exit"
    assert len(rollback_calls) == 1
    assert "filled" in rollback_calls[0][0]
    assert rec.calls == []


@pytest.mark.asyncio
async def test_close_live_position_no_bid_in_book(monkeypatch, patched_live_exit):
    """book есть, но bid отсутствует/0 → отказ (нет цены для SELL floor), rollback резерва."""
    live_exit_mod, rec = patched_live_exit

    async def mock_fetchrow(_sql, *args):
        if "RETURNING" in _sql:
            return _reservation_row()
        return dict(FILLED_ROW)

    async def mock_execute(_sql, *args):
        return "UPDATE 1"

    async def mock_fetch_live_book(_token_id):
        return {"bid": 0.0, "ask": 0.44, "bid_size": 0.0, "ask_size": 500.0}

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(live_exit_mod, "execute", mock_execute)
    monkeypatch.setattr("app.services.execution_safety.fetch_live_book", mock_fetch_live_book)

    ok, msg = await live_exit_mod.close_live_position("order-db-1", reason="manual_tg")

    assert ok is False
    assert msg == "no_book_for_exit"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_close_live_position_sell_price_always_positive(monkeypatch, patched_live_exit):
    """Даже при очень низком bid — SELL price никогда не 0 (tick_size floor 0.01)."""
    live_exit_mod, rec = patched_live_exit

    async def mock_fetchrow(_sql, *args):
        if "RETURNING" in _sql:
            return _reservation_row()
        return dict(FILLED_ROW)

    async def mock_execute(_sql, *args):
        return "UPDATE 1"

    async def mock_fetch_live_book(_token_id):
        return {"bid": 0.005, "ask": 0.01, "bid_size": 100.0, "ask_size": 100.0}

    monkeypatch.setattr(live_exit_mod, "fetchrow", mock_fetchrow)
    monkeypatch.setattr(live_exit_mod, "execute", mock_execute)
    monkeypatch.setattr("app.services.execution_safety.fetch_live_book", mock_fetch_live_book)

    ok, msg = await live_exit_mod.close_live_position("order-db-1", reason="stop_loss")

    assert ok is True
    order_args = rec.calls[0][1]["order_args"]
    assert order_args.price > 0
    assert order_args.price == pytest.approx(0.01)  # floor clamp
