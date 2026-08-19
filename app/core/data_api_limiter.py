"""Межпроцессный rate-limiter для data-api.polymarket.com.

Потребители /trades — отдельные cron-процессы (d26-scan, live-wallet-flow,
live-trade-bucket, trade-poll и др.), каждый со своим semaphore, которые
в сумме давали ~11.8K req/час → 460-680 HTTP 429/час и 47 финальных
провалов/час у d26 (nine-point-audit п.4, замер 2026-07-05). In-memory
семафор между процессами не работает — бюджет делится через Redis
fixed-window счётчик per-second.

Fail-open при недоступном Redis — как остальные Redis-гейты проекта
(check_redis_kill_flag, check_daemon_pause): деградируем в текущее
поведение, не блокируем джобы.
"""

import asyncio
import time

import structlog

logger = structlog.get_logger(__name__)

# Суммарный бюджет всех процессов, req/s. Реальный лимит data-api
# недокументирован; замер 2026-07-05: средний трафик ~3.3 req/s, 429 дают
# всплески (конкурентные пачки перекрывающихся cron-джоб), не средняя.
# 3 rps ≈ текущая средняя пропускная: срезаются только пики, джобы не
# замедляются. polymarket_api_health monitor покажет, исчезли ли 429;
# если нет — понижать (замедлит d26/writer-джобы, но уберёт потери данных).
DATA_API_BUDGET_PER_SEC = 3

# Максимум ожидания слота — защита от live-lock при перегрузке. Дольше
# ждать нет смысла: http_retry поверх и так ретраит сам вызов.
_MAX_WAIT_SEC = 10.0

_KEY_PREFIX = "rl:data_api:"


async def acquire_data_api_slot() -> None:
    """Заблокироваться до свободного слота в общесекундном бюджете data-api.

    INCR на ключе текущей секунды (TTL 2с): значение ≤ бюджета — слот наш;
    иначе спим до начала следующей секунды и пробуем снова. Никогда не
    бросает; ждёт не дольше _MAX_WAIT_SEC.
    """
    from app.core.cache import get_redis

    try:
        redis = await get_redis()
        if redis is None:
            return  # fail-open: Redis недоступен — не блокируем
        deadline = time.monotonic() + _MAX_WAIT_SEC
        while True:
            now = time.time()
            key = f"{_KEY_PREFIX}{int(now)}"
            n = await redis.incr(key)
            await redis.expire(key, 2)
            if int(n) <= DATA_API_BUDGET_PER_SEC:
                return
            if time.monotonic() >= deadline:
                logger.warning("data_api_limiter_wait_exhausted", waited_sec=_MAX_WAIT_SEC)
                return  # fail-open: не давим джобу бесконечно, 429 обработает http_retry
            # до начала следующей секунды (+ крошечный сдвиг от границы)
            await asyncio.sleep(max(0.0, 1.0 - (now % 1.0)) + 0.01)
    except Exception as exc:  # noqa: BLE001 — limiter не роняет data-path
        logger.debug("data_api_limiter_failed", error=str(exc)[:80])
