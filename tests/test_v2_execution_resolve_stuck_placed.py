"""Тесты reanimate зависших FOK-ордеров в статусе 'placed'.

FOK исполняется мгновенно или отклоняется — не должен висеть в 'placed'.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.jobs.v2_execution_resolve as resolve_mod


@pytest.mark.asyncio
async def test_stale_fok_placed_marked_rejected(monkeypatch):
    """FOK старше FOK_STALE_TIMEOUT_SEC, CLOB говорит size_matched=0 → rejected."""
    old = datetime.now(UTC) - timedelta(minutes=10)
    row = {
        "id": "ord-fok-1", "order_id": "clob-1", "signal_price": 0.5,
        "limit_price": 0.5, "stake": 5.0, "order_type": "FOK", "placed_at": old,
    }

    async def _fake_fetch(sql, *args):
        return [row]

    execute_mock = AsyncMock()
    client = MagicMock()
    client.get_order.return_value = {"size_matched": 0, "status": "CANCELED"}
    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", execute_mock)

    counts = await resolve_mod._poll_placed_orders(client)

    assert counts.get("fok_rejected", 0) == 1
    execute_mock.assert_awaited()
    called_sql = execute_mock.await_args.args[0]
    assert "status='rejected'" in called_sql or "status = 'rejected'" in called_sql
    # Regression guard: терминальный rejected без is_transient_reject=true навсегда
    # исключает detector_signal_id из повторных попыток (detector_signal_service.py:152-159).
    # Bookkeeping-баг зависшего FOK — не повод хоронить сигнал.
    assert "is_transient_reject=true" in called_sql or "is_transient_reject = true" in called_sql
    assert "rejection_reason" in called_sql


@pytest.mark.asyncio
async def test_stale_fok_partial_alerts(monkeypatch):
    """FOK старый, size_matched>0 → alert (не rejected), still_open."""
    old = datetime.now(UTC) - timedelta(minutes=10)
    row = {
        "id": "ord-fok-2", "order_id": "clob-2", "signal_price": 0.5,
        "limit_price": 0.5, "stake": 5.0, "order_type": "FOK", "placed_at": old,
    }

    async def _fake_fetch(sql, *args):
        return [row]

    async def _fake_debounce(key, ttl):
        return True

    send_mock = AsyncMock()
    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", AsyncMock())
    monkeypatch.setattr("app.core.task_queue.debounce_acquire", _fake_debounce)
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", send_mock)

    client = MagicMock()
    client.get_order.return_value = {"size_matched": 3.0, "status": "LIVE"}

    counts = await resolve_mod._poll_placed_orders(client)

    assert counts.get("fok_alerted", 0) == 1
    send_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_fok_untouched(monkeypatch):
    """FOK моложе FOK_STALE_TIMEOUT_SEC → still_open, без rejected/alert."""
    fresh = datetime.now(UTC) - timedelta(seconds=10)
    row = {
        "id": "ord-fok-3", "order_id": "clob-3", "signal_price": 0.5,
        "limit_price": 0.5, "stake": 5.0, "order_type": "FOK", "placed_at": fresh,
    }

    async def _fake_fetch(sql, *args):
        return [row]

    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", AsyncMock())
    client = MagicMock()
    client.get_order.return_value = {"size_matched": 0, "status": "LIVE"}

    counts = await resolve_mod._poll_placed_orders(client)

    assert counts.get("fok_rejected", 0) == 0
    assert counts.get("fok_alerted", 0) == 0
    assert counts["still_open"] == 1
