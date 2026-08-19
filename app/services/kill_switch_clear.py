"""Авто-сброс sticky kill-switch для ТРАНЗИЕНТНЫХ причин.

Инцидент 2026-06-28 (4×): `bot:kill_switch` ставится без TTL и нигде не
снимается программно. Транзиентные причины (rate-spike, краткий drift, exposure)
залипают на часы/дни ПОСЛЕ исчезновения причины и повторно блокируют live.

Решение: автоматически снимать флаг ТОЛЬКО для транзиентных причин (по префиксу
значения) и ТОЛЬКО после N подряд чистых циклов. Настоящие фолты (main process
dead, daemon dead, recon_strikes mirror) остаются sticky — manual unlock by
design, чтобы бот не возобновлял торговлю вслепую.

Allowlist намеренно НЕ включает:
  - daemon:main_alive   — после unknown-state простоя не возобновлять торговлю вслепую
    без человека (backend перезапускается сам, restart: always в docker-compose —
    но состояние системы на момент простоя не проверено автоматически)
  - main_self_check:*   — risk-daemon мёртв, defense-in-depth ушёл
  - daemon:recon_strikes — зеркало латчинг-фолта reconcile (broker/DB divergence)
  - любое manual/unknown значение (drill/recovery soak)
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

KILL_SWITCH_KEY = "bot:kill_switch"

# Только reconciliation_drift авто-снимается: orphan/feed-сбой не зависит от
# знаменателя bankroll, поэтому невосприимчив к stale peak_bank/MTM.
# НЕ включены (остаются sticky/manual):
#   - daemon:anomalies — всплеск ордеров = симптом runaway-loop/код-бага;
#   - daemon:exposure, daemon:daily_pnl_hard — знаменатель (peak_bank монотонный,
#     MTM может застареть) может ложно «пройти» при реальной просадке;
#   - daemon:main_alive, daemon:recon_strikes, main_self_check:* — persistent-фолты.
AUTO_CLEARABLE_PREFIXES: tuple[str, ...] = ("reconciliation_drift:",)


def is_auto_clearable(flag_value: str | None) -> bool:
    """True, если причина флага — транзиентная (в allowlist)."""
    if not flag_value:
        return False
    return flag_value.startswith(AUTO_CLEARABLE_PREFIXES)


async def reset_clean_streak(redis, streak_key: str) -> None:
    """Сбросить счётчик чистых циклов (вызывать на любом НЕ-чистом цикле)."""
    if redis is None:
        return
    await redis.delete(streak_key)


async def try_auto_clear(
    redis,
    *,
    streak_key: str,
    threshold: int,
    streak_ttl_sec: int = 600,
    owned_prefixes: tuple[str, ...] | None = None,
) -> bool:
    """Инкремент чистого streak; на threshold — снять флаг, если причина транзиентна.

    Returns True только если флаг реально снят.

    Логика:
      - INCR streak (+ TTL, чтобы старый partial streak не пережил долгий простой);
      - streak < threshold → ещё рано;
      - флага нет (existing is None) → здоровый путь, обнулить streak;
      - причина НЕ транзиентна → НЕ трогать, streak оставить взведённым (sticky фолт);
      - причина транзиентна, но не «своя» (owned_prefixes) → НЕ трогать чужой флаг;
      - иначе → DEL kill_switch + DEL streak, лог + (caller шлёт TG).

    owned_prefixes: если задано, writer снимает ТОЛЬКО флаги со своим префиксом
    (defense-in-depth: reconcile-джоба не трогает daemon-флаг и наоборот).

    Инвариант самодостаточен: streak растёт ТОЛЬКО пока залатчен снимаемый
    «свой» флаг. Нет флага / чужой / persistent → streak обнуляется. Поэтому
    threshold всегда означает «N подряд чистых циклов ПОСЛЕ появления флага»,
    независимо от того, сбросил ли writer streak при set (caller-discipline не
    требуется).
    """
    if redis is None:
        return False

    existing = await redis.get(KILL_SWITCH_KEY)
    existing = existing.decode() if isinstance(existing, bytes) else existing

    # Нет своего снимаемого флага → streak не нужен, обнулить и выйти.
    clearable = (
        existing is not None
        and is_auto_clearable(existing)
        and (owned_prefixes is None or existing.startswith(owned_prefixes))
    )
    if not clearable:
        await redis.delete(streak_key)
        return False

    streak = await redis.incr(streak_key)
    await redis.expire(streak_key, streak_ttl_sec)
    if streak < threshold:
        return False

    await redis.delete(KILL_SWITCH_KEY)
    await redis.delete(streak_key)
    logger.info("kill_switch_auto_cleared", cause=existing, clean_cycles=streak)
    return True
