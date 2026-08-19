"""Тесты _poll_exiting_orders — settle live exit-ордеров (audit топ-1, 2026-07-02).

Live exit path: close_live_position ставит SELL и переводит execution_orders в
status='exiting'. Эта джоба (часть run_v2_execution_resolve, cron */1min) подтягивает
статус SELL-ордера с CLOB и:
  - MATCHED → settled, outcome=EARLY_EXIT, PnL от фактической exit-цены.
  - CANCELLED/EXPIRED → назад в 'filled' (retry следующим тиком), TG-алерт.
"""

from __future__ import annotations

import pytest


def _note_text(call_arg) -> str:
    """Склеивает поля уведомления в текст.

    Джобы отдают в publish() структуру, а не строку, но проверять надо СМЫСЛ
    сообщения — поэтому собираем текст из полей и оставляем assert'ы по
    подстрокам рабочими.
    """
    notes = call_arg if isinstance(call_arg, list) else [call_arg]
    parts = []
    for n in notes:
        parts += [n.headline, *n.what_happened, *(n.evidence or [])]
        parts += [n.what_to_do or "", n.what_if_ignored or ""]
    return " ".join(parts)


from app.jobs import v2_execution_resolve as ver


class _FakeClient:
    def __init__(self, order_resp: dict | None):
        self._resp = order_resp
        self.calls: list[str] = []

    def get_order(self, order_id: str):
        self.calls.append(order_id)
        return self._resp


EXITING_ROW = {
    "id": "order-1",
    "exit_order_id": "sell-oid-1",
    "side": "YES",
    "fill_price": 0.40,
    "stake": 100.0,
    "exit_reason": "take_profit",
    "category": "crypto",
    "fee_schedule": None,
}


@pytest.mark.asyncio
async def test_poll_exiting_no_rows_returns_zero_counts(monkeypatch):
    async def mock_fetch(_sql):
        return []

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    client = _FakeClient(None)

    counts = await ver._poll_exiting_orders(client)

    assert counts == {"settled": 0, "reopened": 0, "still_open": 0, "errors": 0}
    assert client.calls == []


@pytest.mark.asyncio
async def test_poll_exiting_matched_settles_with_pnl(monkeypatch):
    """MATCHED → status=settled, outcome=EARLY_EXIT, PnL считается от exit price."""

    async def mock_fetch(_sql):
        return [dict(EXITING_ROW)]

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)
        return "UPDATE 1"

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr(ver, "execute", mock_execute)

    client = _FakeClient({"status": "MATCHED", "size_matched": 250.0, "original_size": 250.0, "price": 0.60})

    counts = await ver._poll_exiting_orders(client)

    assert counts["settled"] == 1
    assert counts["reopened"] == 0
    assert client.calls == ["sell-oid-1"]

    assert len(captured_updates) == 1
    args = captured_updates[0]
    order_id, exit_price, pnl, fee, slippage, net_pnl = args
    assert order_id == "order-1"
    assert exit_price == pytest.approx(0.60)
    # YES @ fill 0.40, exit 0.60 → gross = stake*(0.60/0.40-1) = 100*0.5 = 50.
    assert pnl == pytest.approx(50.0, abs=0.01)
    assert net_pnl < pnl  # fee+slippage вычтены
    assert fee >= 0
    assert slippage >= 0


@pytest.mark.asyncio
async def test_poll_exiting_partial_fill_over_95pct_settles(monkeypatch):
    """size_matched >= 95% of original_size (тот же порог, что _poll_placed_orders) → settled."""

    async def mock_fetch(_sql):
        return [dict(EXITING_ROW)]

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)
        return "UPDATE 1"

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr(ver, "execute", mock_execute)

    client = _FakeClient({"status": "OPEN", "size_matched": 240.0, "original_size": 250.0, "price": 0.55})

    counts = await ver._poll_exiting_orders(client)

    assert counts["settled"] == 1
    assert len(captured_updates) == 1


@pytest.mark.asyncio
async def test_poll_exiting_cancelled_reopens_position(monkeypatch):
    """CANCELLED (FOK не исполнился) → назад в filled, exit_order_id очищен, TG-алерт."""

    async def mock_fetch(_sql):
        return [dict(EXITING_ROW)]

    captured_updates: list = []
    notify_calls: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)
        return "UPDATE 1"

    async def mock_send_admin_message(text, **kw):
        notify_calls.append(text)

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr(ver, "execute", mock_execute)
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", mock_send_admin_message)

    client = _FakeClient({"status": "CANCELLED", "size_matched": 0.0, "original_size": 250.0})

    counts = await ver._poll_exiting_orders(client)

    assert counts["reopened"] == 1
    assert counts["settled"] == 0
    assert len(captured_updates) == 1
    order_id = captured_updates[0][0]
    assert order_id == "order-1"
    assert len(notify_calls) == 1
    assert "не состоялся" in _note_text(notify_calls[0])


@pytest.mark.asyncio
async def test_poll_exiting_expired_reopens_position(monkeypatch):
    """EXPIRED — тот же путь, что CANCELLED."""

    async def mock_fetch(_sql):
        return [dict(EXITING_ROW)]

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)
        return "UPDATE 1"

    async def mock_send_admin_message(text, **kw):
        pass

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr(ver, "execute", mock_execute)
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", mock_send_admin_message)

    client = _FakeClient({"status": "EXPIRED", "size_matched": 0.0, "original_size": 250.0})

    counts = await ver._poll_exiting_orders(client)

    assert counts["reopened"] == 1
    assert len(captured_updates) == 1


@pytest.mark.asyncio
async def test_poll_exiting_still_open_no_update(monkeypatch):
    """Ни MATCHED ни CANCELLED/EXPIRED (напр. OPEN с size_matched=0) → still_open, без UPDATE."""

    async def mock_fetch(_sql):
        return [dict(EXITING_ROW)]

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)
        return "UPDATE 1"

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr(ver, "execute", mock_execute)

    client = _FakeClient({"status": "OPEN", "size_matched": 0.0, "original_size": 250.0})

    counts = await ver._poll_exiting_orders(client)

    assert counts["still_open"] == 1
    assert captured_updates == []


@pytest.mark.asyncio
async def test_poll_exiting_get_order_exception_counts_error(monkeypatch):
    async def mock_fetch(_sql):
        return [dict(EXITING_ROW)]

    monkeypatch.setattr(ver, "fetch", mock_fetch)

    class _RaisingClient:
        def get_order(self, _order_id):
            raise RuntimeError("network error")

    counts = await ver._poll_exiting_orders(_RaisingClient())

    assert counts["errors"] == 1


@pytest.mark.asyncio
async def test_poll_exiting_none_response_counts_error(monkeypatch):
    async def mock_fetch(_sql):
        return [dict(EXITING_ROW)]

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    client = _FakeClient(None)

    counts = await ver._poll_exiting_orders(client)

    assert counts["errors"] == 1


@pytest.mark.asyncio
async def test_poll_exiting_no_side_side_returns_zero_pnl_but_still_settles(monkeypatch):
    """Guard: неизвестный side (не должно происходить, но не должен крашить)."""

    row = dict(EXITING_ROW)
    row["side"] = "HEDGE"

    async def mock_fetch(_sql):
        return [row]

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)
        return "UPDATE 1"

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr(ver, "execute", mock_execute)

    client = _FakeClient({"status": "MATCHED", "size_matched": 250.0, "original_size": 250.0, "price": 0.60})

    counts = await ver._poll_exiting_orders(client)

    assert counts["settled"] == 1
    pnl = captured_updates[0][2]
    assert pnl == 0.0


@pytest.mark.asyncio
async def test_run_v2_execution_resolve_calls_poll_exiting_when_present(monkeypatch):
    """run_v2_execution_resolve вызывает _poll_exiting_orders только если есть
    exiting-строки (та же экономия round-trip, что и для placed/intent)."""

    calls = {"has_exiting_query": False, "poll_exiting": False}

    async def mock_fetch(sql, *args):
        if "status='exiting'" in sql or 'status = \'exiting\'' in sql:
            calls["has_exiting_query"] = True
            return [{"exists": 1}]
        return []

    async def mock_build_clob_client():
        return _FakeClient(None)

    async def mock_poll_exiting(_client):
        calls["poll_exiting"] = True
        return {"settled": 0, "reopened": 0, "still_open": 0, "errors": 0}

    async def mock_settle_filled():
        return 0

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr(ver, "_build_clob_client", mock_build_clob_client)
    monkeypatch.setattr(ver, "_poll_exiting_orders", mock_poll_exiting)
    monkeypatch.setattr(ver, "_settle_filled", mock_settle_filled)

    result = await ver.run_v2_execution_resolve()

    assert calls["has_exiting_query"] is True
    assert calls["poll_exiting"] is True
    assert "exiting" in result


@pytest.mark.asyncio
async def test_run_v2_execution_resolve_skips_poll_exiting_when_absent(monkeypatch):
    """Нет exiting-строк → _poll_exiting_orders НЕ вызывается (economy round-trip)."""

    poll_exiting_called = []

    async def mock_fetch(_sql, *args):
        return []

    async def mock_poll_exiting(_client):
        poll_exiting_called.append(True)
        return {}

    async def mock_settle_filled():
        return 0

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr(ver, "_poll_exiting_orders", mock_poll_exiting)
    monkeypatch.setattr(ver, "_settle_filled", mock_settle_filled)

    await ver.run_v2_execution_resolve()

    assert poll_exiting_called == []
