"""Test _insert_order error handling — Phase 1 Stage 6.

Покрываем 3 пути:
  - Happy path: fetchrow возвращает row → return str(id).
  - UniqueViolation (existing): return None silently.
  - CheckViolation (new Stage 6): log error + TG alert + return None.

asyncpg mocks для exceptions, structlog capfd для проверки log calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.jobs.v2_execution import _insert_order


def _note_text(call_arg) -> str:
    """Склеивает поля уведомления в текст — assert'ы по подстрокам остаются рабочими."""
    notes = call_arg if isinstance(call_arg, list) else [call_arg]
    parts = []
    for n in notes:
        parts += [n.headline, *n.what_happened, *(n.evidence or [])]
        parts += [n.what_to_do or "", n.what_if_ignored or ""]
    return " ".join(parts)



def _valid_payload(**overrides):
    """Minimal valid payload для _insert_order."""
    defaults = {
        "detector": "D5",
        "detector_signal_id": "00000000-0000-0000-0000-000000000001",
        "market_id": "0xMARKET",
        "event_id": "0xEVT",
        "side": "YES",
        "execution_mode": "virtual",
        "signal_price": 0.40,
        "calibrated_p": 0.55,
        "meta_p": 0.50,
        "ev_pct": 12.5,
        "order_type": "maker",
        "stake": 100.0,
        "bankroll_at_entry": 1000.0,
        "status": "virtual_placed",
        "meta": {},
    }
    defaults.update(overrides)
    return defaults


@pytest.mark.asyncio
async def test_insert_happy_path_returns_id(monkeypatch):
    """fetchrow возвращает row → return str(id)."""
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetchrow",
        AsyncMock(return_value={"id": "abc-id"}),
    )
    result = await _insert_order(_valid_payload())
    assert result == "abc-id"


@pytest.mark.asyncio
async def test_insert_unique_violation_returns_none(monkeypatch):
    """UniqueViolationError → return None silently (existing behaviour)."""
    err = asyncpg.UniqueViolationError("uq_execution_orders_signal")
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetchrow",
        AsyncMock(side_effect=err),
    )
    result = await _insert_order(_valid_payload())
    assert result is None


@pytest.mark.asyncio
async def test_insert_check_violation_returns_none_and_logs(monkeypatch):
    """Phase 1 Stage 6: CheckViolationError → log error + TG alert + return None."""
    err = asyncpg.CheckViolationError("execution_orders_stake_max")
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetchrow",
        AsyncMock(side_effect=err),
    )
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.jobs.v2_execution.publish", notify_mock)

    result = await _insert_order(_valid_payload(stake=999.0))

    assert result is None
    # TG alert вызван с информацией о constraint violation
    notify_mock.assert_called_once()
    call_text = _note_text(notify_mock.call_args[0][0])
    assert "база отвергла запись" in call_text.lower()
    assert "D5" in call_text  # detector в сообщении


@pytest.mark.asyncio
async def test_insert_check_violation_does_not_crash_on_notify_failure(monkeypatch):
    """Если TG send упал → не пропагируем exception (best-effort alert)."""
    err = asyncpg.CheckViolationError("execution_orders_side_whitelist")
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetchrow",
        AsyncMock(side_effect=err),
    )

    async def _notify_fail(*args, **kwargs):
        raise RuntimeError("TG API down")

    monkeypatch.setattr(
        "app.jobs.v2_execution.publish", _notify_fail
    )

    # Не должно крашить
    result = await _insert_order(_valid_payload(side="MAYBE"))
    assert result is None
