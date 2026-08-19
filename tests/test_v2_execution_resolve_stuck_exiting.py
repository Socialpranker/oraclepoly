"""Тесты _reanimate_stuck_exiting — алерт на зависшие exiting-ордера (без авто-отката статуса).

В отличие от _reanimate_stuck_intents (intent ещё не факт дошёл до CLOB, откат безопасен),
для 'exiting' предполагается, что реальный SELL уже размещён на бирже — автоматика не
трогает статус, только эскалирует человеку через send_admin_message. Обычный
_poll_exiting_orders (тот же цикл run_v2_execution_resolve) заберёт settle/reopen, если
статус на CLOB реально изменился.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

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
    def __init__(self, order_resp: dict | None = None, raise_exc: Exception | None = None):
        self._resp = order_resp
        self._raise_exc = raise_exc
        self.calls: list[str] = []

    def get_order(self, order_id: str):
        self.calls.append(order_id)
        if self._raise_exc:
            raise self._raise_exc
        return self._resp


STUCK_ROW = {
    "id": "order-1",
    "exit_order_id": "sell-oid-1",
    "exit_reason": "take_profit",
    "exit_requested_at": "2026-07-02T10:00:00Z",
}


@pytest.mark.asyncio
async def test_no_stuck_exiting_returns_zero_counts(monkeypatch):
    async def mock_fetch(_sql):
        return []

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    client = _FakeClient()

    counts = await ver._reanimate_stuck_exiting(client)

    assert counts == {"alerted": 0}
    assert client.calls == []


@pytest.mark.asyncio
async def test_stuck_exiting_still_open_sends_alert_once(monkeypatch):
    async def mock_fetch(_sql):
        return [dict(STUCK_ROW)]

    debounce_mock = AsyncMock(return_value=True)
    send_alert_mock = AsyncMock()

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr("app.core.task_queue.debounce_acquire", debounce_mock)
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", send_alert_mock)

    client = _FakeClient({"status": "LIVE", "size_matched": 0, "original_size": 250.0})

    counts = await ver._reanimate_stuck_exiting(client)

    assert counts["alerted"] == 1
    send_alert_mock.assert_awaited_once()
    assert "order-1" in _note_text(send_alert_mock.call_args[0][0])


@pytest.mark.asyncio
async def test_stuck_exiting_debounced_no_duplicate_alert(monkeypatch):
    async def mock_fetch(_sql):
        return [dict(STUCK_ROW)]

    debounce_mock = AsyncMock(return_value=False)
    send_alert_mock = AsyncMock()

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr("app.core.task_queue.debounce_acquire", debounce_mock)
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", send_alert_mock)

    client = _FakeClient({"status": "LIVE", "size_matched": 0, "original_size": 250.0})

    counts = await ver._reanimate_stuck_exiting(client)

    assert counts["alerted"] == 0
    send_alert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_stuck_exiting_get_order_exception_still_alerts(monkeypatch):
    """CLOB недоступен (exception) — тоже считается "не смогли подтвердить", алертим."""

    async def mock_fetch(_sql):
        return [dict(STUCK_ROW)]

    debounce_mock = AsyncMock(return_value=True)
    send_alert_mock = AsyncMock()

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr("app.core.task_queue.debounce_acquire", debounce_mock)
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", send_alert_mock)

    client = _FakeClient(raise_exc=RuntimeError("network timeout"))

    counts = await ver._reanimate_stuck_exiting(client)

    assert counts["alerted"] == 1
    send_alert_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_stuck_exiting_recovered_status_skips_alert(monkeypatch):
    """Статус на CLOB уже сменился (MATCHED/CANCELLED) — не наш случай, обычный
    _poll_exiting_orders заберёт settle/reopen в этом же тике, здесь только пропускаем."""

    async def mock_fetch(_sql):
        return [dict(STUCK_ROW)]

    debounce_mock = AsyncMock(return_value=True)
    send_alert_mock = AsyncMock()

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr("app.core.task_queue.debounce_acquire", debounce_mock)
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", send_alert_mock)

    client = _FakeClient({"status": "MATCHED", "size_matched": 250.0, "original_size": 250.0})

    counts = await ver._reanimate_stuck_exiting(client)

    assert counts["alerted"] == 0
    send_alert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_stuck_exiting_does_not_mutate_status(monkeypatch):
    """Design constraint: функция никогда не вызывает execute() — только SELECT/алерт.
    Авто-откат exiting -> filled опасен, если SELL реально жив на бирже."""

    async def mock_fetch(_sql):
        return [dict(STUCK_ROW)]

    execute_mock = AsyncMock()
    debounce_mock = AsyncMock(return_value=True)
    send_alert_mock = AsyncMock()

    monkeypatch.setattr(ver, "fetch", mock_fetch)
    monkeypatch.setattr(ver, "execute", execute_mock)
    monkeypatch.setattr("app.core.task_queue.debounce_acquire", debounce_mock)
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", send_alert_mock)

    client = _FakeClient({"status": "LIVE", "size_matched": 0, "original_size": 250.0})

    await ver._reanimate_stuck_exiting(client)

    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_v2_execution_resolve_includes_stuck_exiting_counts(monkeypatch):
    """Интеграция в run_v2_execution_resolve: новый ключ counts['stuck_exiting']."""

    async def mock_fetch(sql, *args):
        if "status='exiting' AND exit_requested_at" in sql or "status = 'exiting' AND exit_requested_at" in sql:
            return []
        return []

    monkeypatch.setattr(ver, "fetch", mock_fetch)

    result = await ver.run_v2_execution_resolve()

    assert "stuck_exiting" in result
