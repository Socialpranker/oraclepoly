#!/usr/bin/env python3
"""
Oracle Worker — Redis queue consumer для intelligence-пайплайна.

4 параллельных consumer-корутин:
- transactions: обработка whale-транзакций
- recalculate: пересчёт Oracle Score (debounce 60s)
- notifications: рассылка алертов
- analysis: генерация AI-анализа (debounce 30 мин)

Usage:
    python scripts/oracle_worker.py
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("oracle_worker")

# Конфигурация очередей
# Phoenix V2 (2026-04-30): убрана queue:recalculate (oracle стал feature, не gate).
QUEUES = {
    "oracle:queue:transactions": {
        "handler": "handle_transaction",
        "concurrency": 5,
        "debounce_key": None,
        "debounce_ttl": 0,
    },
}

MAX_RETRIES = 3
RETRY_DELAYS = [2, 4, 8]

# Человекочитаемые описания очередей для алертов
QUEUE_DESCRIPTIONS = {
    "oracle:queue:transactions": "🐋 Whale transactions — обработка транзакций, whale_watcher",
}


class OracleWorker:
    """Redis queue worker с graceful shutdown и retry."""

    def __init__(self):
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._rtds_task: asyncio.Task | None = None

    async def start(self):
        """Инициализация DB/Redis и запуск consumer-корутин."""
        from app.core.cache import init_redis
        from app.core.config import get_settings
        from app.services.database import init_db

        settings = get_settings()
        await init_db()
        await init_redis(settings.redis_url)

        self._running = True
        logger.info("oracle_worker_started, queues=%d", len(QUEUES))
        try:
            from app.tasks.notifications import send_admin_message
            await send_admin_message("✅ <b>Oracle Worker started</b>\nОчереди: " + ", ".join(QUEUES.keys()))
        except Exception:
            pass

        for queue_name, config in QUEUES.items():
            sem = asyncio.Semaphore(config["concurrency"])
            self._semaphores[queue_name] = sem
            task = asyncio.create_task(
                self._consume(queue_name, config),
                name=f"consumer:{queue_name}",
            )
            self._tasks.append(task)

        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self):
        """Graceful shutdown: прекращаем приём, ждём текущие задачи 30s."""
        logger.info("oracle_worker_stopping")
        self._running = False

        # Ждём завершения текущих задач (максимум 30с)
        for task in self._tasks:
            task.cancel()

        if self._rtds_task and not self._rtds_task.done():
            self._rtds_task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        from app.core.cache import close_redis
        from app.services.database import close_db

        await close_db()
        await close_redis()
        logger.info("oracle_worker_stopped")

    async def _consume(self, queue_name: str, config: dict):
        """Бесконечный цикл: BRPOP → debounce → handler → retry."""
        from app.core.task_queue import debounce_acquire, dequeue

        handler_name = config["handler"]
        handler = getattr(self, handler_name)
        sem = self._semaphores[queue_name]
        debounce_key_tpl = config["debounce_key"]
        debounce_ttl = config["debounce_ttl"]

        while self._running:
            try:
                data = await dequeue(queue_name, timeout=1)
                if data is None:
                    continue

                # Debounce check
                if debounce_key_tpl and debounce_ttl:
                    key = debounce_key_tpl.format(**data)
                    acquired = await debounce_acquire(key, debounce_ttl)
                    if not acquired:
                        logger.debug("debounced: queue=%s key=%s", queue_name, key)
                        continue

                # Выполнение с semaphore и retry
                async with sem:
                    await self._execute_with_retry(handler, data, queue_name)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("consumer_loop_error: queue=%s error=%s", queue_name, str(e))
                try:
                    from app.tasks.notifications import send_admin_message
                    desc = QUEUE_DESCRIPTIONS.get(queue_name, queue_name)
                    await send_admin_message(
                        f"⚠️ <b>Worker consumer loop error</b>\n"
                        f"<b>Очередь:</b> {queue_name}\n"
                        f"<b>Что это:</b> {desc}\n"
                        f"<b>Ошибка:</b> <code>{str(e)[:300]}</code>\n"
                        f"<i>Worker продолжает работу (sleep 1s)</i>"
                    )
                except Exception:
                    pass
                await asyncio.sleep(1)

    async def _execute_with_retry(self, handler, data: dict, queue_name: str):
        """Выполнить handler с retry (3 попытки, backoff 2s/4s/8s)."""
        import json

        for attempt in range(MAX_RETRIES):
            try:
                await handler(data)
                return
            except Exception as e:
                logger.warning(
                    "handler_retry: queue=%s attempt=%d/%d error=%s",
                    queue_name, attempt + 1, MAX_RETRIES, str(e),
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAYS[attempt])

        # Dead letter
        logger.error("handler_dead_letter: queue=%s data=%s", queue_name, str(data)[:500])
        try:
            from app.core.task_queue import _get_redis

            redis = _get_redis()
            if redis:
                dead_key = f"oracle:dead:{queue_name}"
                await redis.lpush(dead_key, json.dumps(data, default=str))
        except Exception as e:
            logger.error("dead_letter_write_failed", error=str(e))

        # TG-алерт: handler упал 3 раза, сообщение в dead letter
        try:
            from app.tasks.notifications import send_admin_message
            desc = QUEUE_DESCRIPTIONS.get(queue_name, queue_name)
            market_id = data.get("market_id", "")
            tx_hash = data.get("tx_hash", "")
            context = f"market_id={market_id}" if market_id else (f"tx_hash={tx_hash}" if tx_hash else "")
            await send_admin_message(
                f"💀 <b>Dead letter: handler упал {MAX_RETRIES} раза</b>\n"
                f"<b>Очередь:</b> {queue_name}\n"
                f"<b>Что это:</b> {desc}\n"
                + (f"<b>Контекст:</b> <code>{context}</code>\n" if context else "")
                + f"<b>Данные:</b> <code>{json.dumps(data, default=str)[:400]}</code>"
            )
        except Exception:
            pass

    # === Handlers ===

    async def handle_transaction(self, data: dict):
        """Обработка whale-транзакции."""
        from app.tasks.transaction import process_transaction_event

        result = await process_transaction_event(data)
        logger.info(
            "transaction_processed: tx_hash=%s status=%s",
            data.get("tx_hash"),
            result.get("status"),
        )

def main():
    worker = OracleWorker()
    loop = asyncio.new_event_loop()

    def handle_signal(sig, _frame):
        logger.info("received_signal: %s", sig)
        loop.create_task(worker.stop())

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        loop.run_until_complete(worker.start())
    except KeyboardInterrupt:
        loop.run_until_complete(worker.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
