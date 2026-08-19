"""Резидентный execution-демон (Phase 3 Task 3, audit топ-7).

Money-path раньше жил только на минутном cron: нижняя граница сигнал→ордер
1-4 минуты, каждый тик — fork+import+init. Демон закрывает этот разрыв: сканы
(d3_scan и т.д.) кладут id только что вставленного сигнала в Redis-очередь
oracle:queue:execution_signals → демон забирает его BRPOP'ом (блокирующее
ожидание, без polling-спама) и прогоняет через v2_execution._process_signal
немедленно. Cron v2-execution остаётся watchdog-проходом для сигналов,
которые демон по любой причине пропустил (Redis flap, рестарт).

Двойная защита от гонки демон-vs-cron: partial UNIQUE constraint на
execution_orders (Phase 1) + cooldown-фильтр в _SIGNAL_SELECT_BASE_SQL —
второй insert того же сигнала (демон уже обработал, cron подхватил снова)
падает в idempotency-гейт, не дублирует ордер.

Включение: EXECUTION_DAEMON_ENABLED=true (по умолчанию False — демон-процесс
запускается всегда как Docker service, но главный цикл проверяет флаг перед
каждым dequeue и при False только спит, ничего не исполняя).

Запуск:
  python scripts/execution_daemon.py --loop          # production
  python scripts/execution_daemon.py --check-once    # dev/CI/healthcheck

Структура (init/loop/graceful shutdown) — по образцу scripts/risk_daemon.py.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import structlog

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.cache import close_redis, init_redis  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.task_queue import dequeue  # noqa: E402
from app.services.database import close_db, init_db  # noqa: E402
from app.services.detector_signal_service import fetch_calibrated_signal_by_id  # noqa: E402

logger = structlog.get_logger(__name__)

QUEUE = "oracle:queue:execution_signals"

# Демон — money-path процесс, не web-сервер: маленький пул достаточен
# (один сигнал за раз обрабатывается последовательно из BRPOP).
_DB_POOL_MIN_SIZE = 1
_DB_POOL_MAX_SIZE = 3

# Пока execution_daemon_enabled=False, цикл не делает BRPOP вовсе (не блокирует
# закрытие процесса зря) — просто спит и перечитывает флаг раз в 30с.
_DISABLED_POLL_INTERVAL_SEC = 30


async def process_one(signal_id: str) -> str:
    """Один сигнал: выборка + fetch_calibrated_signal_by_id (та же age/cooldown/
    stage фильтрация, что у cron) → v2_execution._process_signal.

    Returns: результат _process_signal ('opened'|'rejected'|'skipped'|'duplicate')
    либо 'gone' если сигнал уже отфильтрован (протух/cooldown/stage сменился —
    не ошибка, просто нечего делать).
    """
    sig = await fetch_calibrated_signal_by_id(signal_id)
    if sig is None:
        logger.debug("exec_daemon_signal_gone", signal_id=signal_id)
        return "gone"
    # Отложенный импорт: v2_execution тянет тяжёлые calibration-зависимости,
    # не нужные при простое (import cost платится только на первый сигнал).
    from app.jobs import v2_execution

    # Fix-2: все insert'ы этого пути тегируются 'daemon' в execution_orders.meta
    # (default contextvar'а — 'cron', его ставить не нужно).
    v2_execution.EXECUTION_SOURCE.set("daemon")
    result = await v2_execution._process_signal(sig)
    logger.info("exec_daemon_processed", signal_id=signal_id, result=result)
    return result


async def main_loop() -> None:
    """Production loop: init connections → while True (флаг-гейт + BRPOP + process)."""
    settings = get_settings()
    await init_db(min_size=_DB_POOL_MIN_SIZE, max_size=_DB_POOL_MAX_SIZE)
    await init_redis(settings.redis_url)
    try:
        logger.info("exec_daemon_started", enabled=settings.execution_daemon_enabled)
        while True:
            if not get_settings().execution_daemon_enabled:
                await asyncio.sleep(_DISABLED_POLL_INTERVAL_SEC)
                continue
            item = await dequeue(QUEUE, timeout=5)
            if item is None:
                continue
            signal_id = item.get("detector_signal_id")
            if not signal_id:
                logger.warning("exec_daemon_malformed_item", item=item)
                continue
            try:
                await process_one(signal_id)
            except Exception as e:
                logger.error("exec_daemon_error", signal_id=signal_id, error=str(e)[:300])
    finally:
        await close_db()
        await close_redis()


async def check_once() -> dict:
    """One-shot для CI / Docker healthcheck: init connections, no dequeue loop."""
    settings = get_settings()
    await init_db(min_size=_DB_POOL_MIN_SIZE, max_size=_DB_POOL_MAX_SIZE)
    await init_redis(settings.redis_url)
    try:
        return {"status": "ok", "enabled": settings.execution_daemon_enabled}
    finally:
        await close_db()
        await close_redis()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Production: continuous loop")
    parser.add_argument("--check-once", action="store_true", help="One-shot check, exit")
    args = parser.parse_args()

    if args.loop:
        asyncio.run(main_loop())
    elif args.check_once:
        result = asyncio.run(check_once())
        print(result)
        sys.exit(0 if result.get("status") == "ok" else 1)
    else:
        parser.print_help()
        sys.exit(1)
