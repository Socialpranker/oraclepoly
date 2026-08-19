"""Heart-beat dead-man для main process — Phase 1 Stage 5.

Симметричный protocol с risk_daemon:
  - Main process пишет bot:risk_heartbeat=now() каждые N сек.
  - Main читает bot:risk_daemon_alive — если stale > threshold → SET kill_switch
    (daemon мёртв → defense-in-depth исчез → останавливаемся).

См. docs/superpowers/plans/2026-05-risk-engine-hardening/05-risk-daemon-deadman.md
(Step 5.1 — Heartbeat writer в main process).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from app.core.cache import get_redis
from app.core.config import get_settings

logger = structlog.get_logger(__name__)

MAIN_HEARTBEAT_KEY = "bot:risk_heartbeat"
DAEMON_HEARTBEAT_KEY = "bot:risk_daemon_alive"
KILL_SWITCH_KEY = "bot:kill_switch"


async def write_main_heartbeat() -> None:
    """Одиночная запись heartbeat — обёртка для тестируемости."""
    redis = await get_redis()
    if redis is None:
        return
    settings = get_settings()
    await redis.setex(
        MAIN_HEARTBEAT_KEY,
        settings.risk_daemon_main_stale_threshold_sec,
        datetime.now(UTC).isoformat(),
    )


async def check_daemon_alive() -> tuple[bool, str]:
    """True если bot:risk_daemon_alive свежее threshold.

    Returns (False, reason) если daemon мёртв либо heartbeat malformed.
    Redis недоступен → (True, "") (conservative).
    """
    redis = await get_redis()
    if redis is None:
        return True, ""
    settings = get_settings()
    raw = await redis.get(DAEMON_HEARTBEAT_KEY)
    if not raw:
        return False, "risk_daemon_heartbeat_missing"
    try:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - last).total_seconds()
        if age > settings.risk_daemon_main_stale_threshold_sec:
            return False, f"risk_daemon_dead:age={age:.0f}s"
    except (ValueError, TypeError):
        return False, "risk_daemon_heartbeat_malformed"
    return True, ""


async def set_kill_if_daemon_dead() -> bool:
    """Single iteration check. True если установил kill_switch."""
    ok, reason = await check_daemon_alive()
    if ok:
        return False
    redis = await get_redis()
    if redis is None:
        return False
    existing = await redis.get(KILL_SWITCH_KEY)
    if existing:
        return False  # preserve first cause
    await redis.set(KILL_SWITCH_KEY, f"main_self_check:{reason}")
    logger.error("main_self_check_kill_set", reason=reason)

    from app.tasks.notifications import send_admin_message

    await send_admin_message(
        f"🛑 <b>KILL SWITCH: risk_daemon мёртв</b>\nReason: <code>{reason}</code>\n"
        "Defense-in-depth демон не пишет heartbeat — main process остановил торговлю.\n"
        "Проверь risk_daemon (перезапуск), затем сними kill switch вручную."
    )
    return True


async def heartbeat_writer_loop() -> None:
    """Long-running task: пишет main heartbeat каждые N сек.

    Запускается в lifespan startup, cancel'нится в shutdown.
    """
    settings = get_settings()
    interval = settings.risk_daemon_heartbeat_interval_sec
    logger.info("heartbeat_writer_started", interval_sec=interval)
    while True:
        try:
            await write_main_heartbeat()
        except Exception as e:
            logger.warning("heartbeat_writer_error", error=str(e))
        await asyncio.sleep(interval)


async def main_self_check_loop() -> None:
    """Long-running task: каждые N сек проверяем что risk_daemon жив."""
    settings = get_settings()
    # Self-check реже чем heartbeat write (60s default vs 30s) — даём
    # daemon шанс восстановиться после restart.
    interval = max(60, settings.risk_daemon_check_interval_sec * 2)
    logger.info("main_self_check_started", interval_sec=interval)
    while True:
        try:
            await set_kill_if_daemon_dead()
        except Exception as e:
            logger.warning("main_self_check_error", error=str(e))
        await asyncio.sleep(interval)
