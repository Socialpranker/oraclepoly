"""Тесты execution-демона (Phase 3 Task 3, audit топ-7).

Демон целиком (main_loop: init_db/init_redis/BRPOP-цикл) не тестируем —
слишком много side effects (реальные подключения). Тестируем чистую функцию
process_one с мокнутыми fetch_calibrated_signal_by_id / _process_signal, плюс
флаг-гейт execution_daemon_enabled (главный цикл не должен исполнять сигналы
при выключенном флаге).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from scripts.execution_daemon import QUEUE, process_one


@pytest.mark.asyncio
async def test_process_one_calls_process_signal_with_fetched_sig():
    """Сигнал найден → _process_signal вызван с ним, результат прокинут наружу."""
    sig = {"detector": "D3_cross_mkt_arb", "detector_signal_id": "sig-1", "token_id": "tok-1"}

    with (
        patch(
            "scripts.execution_daemon.fetch_calibrated_signal_by_id",
            AsyncMock(return_value=sig),
        ),
        patch("app.jobs.v2_execution._process_signal", AsyncMock(return_value="opened")) as process_mock,
    ):
        result = await process_one("sig-1")

    assert result == "opened"
    process_mock.assert_awaited_once_with(sig)


@pytest.mark.asyncio
async def test_process_one_skips_silently_when_signal_gone():
    """Сигнал уже отфильтрован основой (протух/cooldown/stage) → 'gone', без вызова _process_signal."""
    with (
        patch(
            "scripts.execution_daemon.fetch_calibrated_signal_by_id",
            AsyncMock(return_value=None),
        ),
        patch("app.jobs.v2_execution._process_signal", AsyncMock()) as process_mock,
    ):
        result = await process_one("sig-gone")

    assert result == "gone"
    process_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_one_propagates_rejected_result():
    """_process_signal вернул 'rejected' (safety-гейт отбил) → проброшено как есть."""
    sig = {"detector": "D3_cross_mkt_arb", "detector_signal_id": "sig-2", "token_id": "tok-2"}

    with (
        patch(
            "scripts.execution_daemon.fetch_calibrated_signal_by_id",
            AsyncMock(return_value=sig),
        ),
        patch("app.jobs.v2_execution._process_signal", AsyncMock(return_value="rejected")),
    ):
        result = await process_one("sig-2")

    assert result == "rejected"


# --- Флаг-гейт execution_daemon_enabled ------------------------------------
# Главный цикл ДОЛЖЕН проверять флаг перед каждым dequeue — демон-процесс
# запускается всегда (Docker service), но при default False не должен молча
# исполнять сигналы из очереди. Тестируем через асинхронный генератор,
# который останавливает бесконечный while True после N итераций
# (StopAsyncIteration из мока sleep/dequeue) — чище, чем реальный while True.


@pytest.mark.asyncio
async def test_main_loop_does_not_dequeue_when_daemon_disabled():
    """execution_daemon_enabled=False → dequeue НИ РАЗУ не вызван (сон + continue)."""
    import scripts.execution_daemon as daemon

    settings_stub = type("S", (), {"execution_daemon_enabled": False, "redis_url": "redis://x"})()
    dequeue_mock = AsyncMock(return_value=None)

    class _StopLoop(Exception):
        pass

    sleep_mock = AsyncMock(side_effect=[None, _StopLoop()])  # 2-я итерация обрывает while True

    with (
        patch("scripts.execution_daemon.get_settings", lambda: settings_stub),
        patch("scripts.execution_daemon.init_db", AsyncMock()),
        patch("scripts.execution_daemon.init_redis", AsyncMock()),
        patch("scripts.execution_daemon.close_db", AsyncMock()),
        patch("scripts.execution_daemon.close_redis", AsyncMock()),
        patch("scripts.execution_daemon.dequeue", dequeue_mock),
        patch("scripts.execution_daemon.asyncio.sleep", sleep_mock),
        pytest.raises(_StopLoop),
    ):
        await daemon.main_loop()

    dequeue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_loop_dequeues_when_daemon_enabled():
    """execution_daemon_enabled=True → dequeue вызывается (никакого сна-пропуска)."""
    import scripts.execution_daemon as daemon

    settings_stub = type("S", (), {"execution_daemon_enabled": True, "redis_url": "redis://x"})()

    class _StopLoop(Exception):
        pass

    dequeue_mock = AsyncMock(side_effect=[None, _StopLoop()])

    with (
        patch("scripts.execution_daemon.get_settings", lambda: settings_stub),
        patch("scripts.execution_daemon.init_db", AsyncMock()),
        patch("scripts.execution_daemon.init_redis", AsyncMock()),
        patch("scripts.execution_daemon.close_db", AsyncMock()),
        patch("scripts.execution_daemon.close_redis", AsyncMock()),
        patch("scripts.execution_daemon.dequeue", dequeue_mock),
        pytest.raises(_StopLoop),
    ):
        await daemon.main_loop()

    assert dequeue_mock.await_count == 2


def test_queue_name_constant():
    """QUEUE — единый ключ, которым d3_scan enqueue'ит и демон dequeue'ит."""
    assert QUEUE == "oracle:queue:execution_signals"
