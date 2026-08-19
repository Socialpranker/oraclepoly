"""Тесты для app/core/d45_loop.py.

Покрытие:
- Один тик scan вызван.
- stop_event останавливает loop.
- Exception в scan не валит loop, продолжает следующую итерацию.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.core import d45_loop as loop_mod


@pytest.fixture(autouse=True)
def _patch_settings():
    """Settings с короткими интервалами для скорости тестов."""
    fake_settings = type("S", (), {"d45_chainlink_leadlag_scan_interval_sec": 0.01})()
    with patch("app.core.d45_loop.get_settings", return_value=fake_settings):
        yield


@pytest.mark.asyncio
async def test_loop_invokes_scan_and_stops_on_event():
    """Loop делает хотя бы 1 scan, потом завершается при stop_event."""
    scan_mock = AsyncMock(return_value={"scanned": 0, "signals": 0})
    stop_event = asyncio.Event()

    with patch("app.core.d45_loop.run_d45_chainlink_leadlag_scan", scan_mock):
        task = asyncio.create_task(loop_mod.d45_chainlink_leadlag_loop(stop_event))
        # Дать loop успеть стартануть и хотя бы один scan сделать
        await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert scan_mock.await_count >= 1


@pytest.mark.asyncio
async def test_loop_continues_after_scan_exception():
    """Exception в scan → loop логирует warning, не падает, следующий scan."""
    call_count = 0
    # Ждём сам факт второго вызова, а не сон по стенным часам: на загруженном
    # раннере двух проходов за фиксированный sleep может не случиться (флейк CI).
    second_call = asyncio.Event()

    async def flaky_scan():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient failure")
        second_call.set()
        return {"scanned": 0, "signals": 0}

    stop_event = asyncio.Event()
    with patch("app.core.d45_loop.run_d45_chainlink_leadlag_scan", side_effect=flaky_scan):
        task = asyncio.create_task(loop_mod.d45_chainlink_leadlag_loop(stop_event))
        await asyncio.wait_for(second_call.wait(), timeout=5.0)
        stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

    assert call_count >= 2  # хотя бы один scan после exception


@pytest.mark.asyncio
async def test_loop_cancel_propagates():
    """task.cancel() → loop завершается, без зависания."""
    scan_mock = AsyncMock(return_value={"scanned": 0, "signals": 0})

    with patch("app.core.d45_loop.run_d45_chainlink_leadlag_scan", scan_mock):
        task = asyncio.create_task(loop_mod.d45_chainlink_leadlag_loop())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_loop_no_stop_event_passed_works():
    """stop_event=None → loop работает до внешнего cancel()."""
    scan_mock = AsyncMock(return_value={"scanned": 0, "signals": 0})

    with patch("app.core.d45_loop.run_d45_chainlink_leadlag_scan", scan_mock):
        task = asyncio.create_task(loop_mod.d45_chainlink_leadlag_loop())
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert scan_mock.await_count >= 1
