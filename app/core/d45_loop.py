"""D45 Chainlink lead-lag scan loop — фоновый task внутри FastAPI lifespan.

Почему НЕ cron:
    Полный цикл detect→signal должен укладываться в 5-секундный tick, а cron
    минимальная гранулярность 1 минута. Дробить цикл на 12 итераций внутри
    одной cron-минуты возможно, но тогда каждая cron-команда поднимает свежий
    PythStreamService → 60s connection setup → 1-2 валидных тика. Это ломает
    весь смысл lead-lag (нужны сотни ticks/min).

Решение:
    Singleton PythStreamService живёт в backend container'е (lifespan start).
    Параллельный asyncio task делает scan каждые N сек, не блокируя API.

Поведение при shutdown:
    Stop event сигналит loop'у завершиться на следующем sleep. Текущий scan
    дорабатывает до конца (≤ 1 sec в типичном случае).

Поведение при scan exception:
    Логируем warning, ждём interval, продолжаем — никогда не падаем.
    Один проблемный market не должен останавливать всю петлю.
"""

from __future__ import annotations

import asyncio

import structlog

from app.core.config import get_settings
from app.jobs.d45_chainlink_leadlag_scan import run_d45_chainlink_leadlag_scan

logger = structlog.get_logger(__name__)


async def d45_chainlink_leadlag_loop(stop_event: asyncio.Event | None = None) -> None:
    """Long-running scan loop.

    Args:
        stop_event: asyncio.Event для graceful shutdown. None → внутренний
            (loop работает до cancel()).
    """
    settings = get_settings()
    interval = settings.d45_chainlink_leadlag_scan_interval_sec
    logger.info("d45_chainlink_leadlag_loop_started", interval_sec=interval)

    own_stop = stop_event is None
    if stop_event is None:
        stop_event = asyncio.Event()

    try:
        while not stop_event.is_set():
            try:
                await run_d45_chainlink_leadlag_scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Никогда не падаем: один сбойный scan не должен останавливать
                # весь loop. Конкретная market проблема обрабатывается внутри
                # run_d45_chainlink_leadlag_scan через per-market try/except.
                logger.warning("d45_chainlink_leadlag_loop_scan_error", error=str(exc))

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                # Это норма — таймаут wait_for(stop_event) и есть наш sleep.
                continue
    finally:
        if own_stop:
            stop_event.set()
        logger.info("d45_chainlink_leadlag_loop_stopped")
