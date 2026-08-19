"""Peak bankroll tracking через Redis.

Phase 1 Stage 2. См. docs/superpowers/plans/2026-05-risk-engine-hardening/02-risk-engine-integration.md.

Используется в [[risk-engine]].check_risk(peak_bank=…) для drawdown reduce:
  если current bank < threshold × peak → stake × drawdown_factor.

Storage: Redis key 'bot:risk:peak_bank_usd' (float as string).
Persistence: pure Redis — потеря Redis сбрасывает peak (приемлемо: реальное
real-money exposure tracked в execution_orders + wallet_resolver, peak —
только heuristic для drawdown gate).

mtm-refresh cron в конце своего цикла вызывает update_peak_if_higher
с текущим MtM equity. Это гарантирует frequent updates без race condition.
"""

from __future__ import annotations

import structlog

from app.core.cache import get_redis

logger = structlog.get_logger(__name__)

PEAK_BANK_KEY = "bot:risk:peak_bank_usd"

# Задача D (2026-07-03): PEAK_BANK_KEY застрял на историческом максимуме из эпохи большого банка
# (daemon-exposure знаменатель — #621, не трогать). risk_engine-drawdown нужен
# ОТДЕЛЬНЫЙ peak, засеянный от текущего live bankroll, иначе stale-high peak
# создаёт вечный "большая просадка" артефакт и режет каждый live-стейк ×0.5.
LIVE_PEAK_BANK_KEY = "bot:risk:live_peak_bank_usd"


async def get_peak_bank(*, key: str = PEAK_BANK_KEY) -> float | None:
    """Read stored peak bank. None если Redis недоступен или key не установлен."""
    redis = await get_redis()
    if redis is None:
        return None
    raw = await redis.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("peak_bank_corrupt_value", raw=raw, key=key)
        return None


async def update_peak_if_higher(current_bank: float, *, key: str = PEAK_BANK_KEY) -> float:
    """GREATEST(stored_peak, current_bank) → store + return new peak.

    Защита:
      - current_bank <= 0 → no-op, return stored peak (или current если Redis down).
      - Redis down → return current_bank как fallback peak.
      - Stored peak отсутствует → seed = current_bank.

    Returns: новый peak (после update). Caller использует для check_risk.
    """
    if current_bank <= 0:
        # Corrupt input — не паникуем, возвращаем stored или 0.
        stored = await get_peak_bank(key=key)
        return stored if stored is not None else 0.0

    redis = await get_redis()
    if redis is None:
        # Conservative: используем current как peak (нет history).
        return current_bank

    raw = await redis.get(key)
    stored_peak: float | None
    try:
        stored_peak = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        stored_peak = None

    if stored_peak is None or current_bank > stored_peak:
        await redis.set(key, str(current_bank))
        return current_bank
    return stored_peak
