"""
Calibration Service — загрузка весов для Bayesian Oracle.

3-tier fallback: Redis cache (TTL 600s) → PostgreSQL → hardcoded defaults.
"""

import structlog

from app.core.cache import cache_delete_pattern, cache_get, cache_set
from app.repositories.calibration_repository import CalibrationRepository

logger = structlog.get_logger()

# Hardcoded fallback — синхронизировано с прод calibration_configs global (2026-04-05)
# и scoring.py DEFAULT_WEIGHTS. ⚠️ Не менять одно без другого.
_FALLBACK_CALIBRATION = {
    "w_mkt": 0.6165,
    "w_whale": 0.5,
    "w_whale_contra": 0.0,
    "w_fresh": 0.0,
    "w_cluster": 0.0,
    "w_registry": 0.1,
    "w_oi": 0.2,
    "w_concentration": 0.0,
    "w_micro": 0.0,  # micro_factor удалён, всегда 0.0
    "w_sports_odds": 0.0,
    "w_retail": 0.0,
    "w_maker_direction": 0.5,  # taker flow direction (corr +0.76, поднят с 0.2)
    "w_taker_imbalance": 0.0,
    "temperature": 0.826,
    "beta_a": 1.7719,
    "beta_b": -0.2684,
    "calibration_method": "beta",
    "source": "fallback",
}

CACHE_TTL = 600  # 10 минут


async def get_active_calibration(category: str = "global") -> dict:
    """Получить активную калибровку: Redis → DB → (fallback to global) → hardcoded.

    Fallback chain:
    1. Redis cache calibration:{category}
    2. DB: category config
    3. Если category != "global": повторить 1-2 с "global"
    4. Hardcoded _FALLBACK_CALIBRATION
    """
    result = await _try_load_calibration(category)
    if result:
        return result

    # Fallback на global если запрашивали конкретную категорию
    if category != "global":
        result = await _try_load_calibration("global")
        if result:
            return result

    logger.warning("calibration_using_fallback", category=category)
    return _FALLBACK_CALIBRATION.copy()


async def _try_load_calibration(category: str) -> dict | None:
    """Попытка загрузить калибровку: Redis → DB. Возвращает None если не найдено."""
    cache_key = f"calibration:{category}"

    # Redis cache
    cached = await cache_get(cache_key)
    if cached:
        return cached

    # PostgreSQL
    try:
        repo = CalibrationRepository()
        config = await repo.get_active(category)

        if config:
            result = {
                "w_mkt": float(config.get("w_mkt", 0.6165)),
                "w_whale": float(config.get("w_whale", 0.5)),
                "w_whale_contra": float(config.get("w_whale_contra", 0.0)),
                "w_fresh": float(config.get("w_fresh", 0.0)),
                "w_cluster": float(config.get("w_cluster", 0.0)),
                "w_registry": float(config.get("w_registry", 0.1)),
                "w_oi": float(config.get("w_oi", 0.2)),
                "w_concentration": float(config.get("w_concentration", 0.0)),
                "w_micro": float(config.get("w_micro", 0.0)),
                "w_sports_odds": float(config.get("w_sports_odds", 0.0)),
                "w_retail": float(config.get("w_retail", 0.0)),
                "w_maker_direction": float(config.get("w_maker_direction", 0.2)),
                "w_taker_imbalance": float(config.get("w_taker_imbalance", 0.0)),
                "temperature": float(config.get("temperature", 0.826)),
                "beta_a": float(config.get("beta_a", 1.7719)),
                "beta_b": float(config.get("beta_b", -0.2684)),
                "calibration_method": config.get("calibration_method", "beta"),
                "source": config.get("source", "db"),
                "config_id": config["id"],
            }
            await cache_set(cache_key, result, ttl_seconds=CACHE_TTL)
            return result
    except Exception as e:
        logger.warning("calibration_db_load_failed", category=category, error=str(e))

    return None


def get_weights_dict(calibration: dict) -> dict[str, float]:
    """Конвертировать калибровку в формат BayesianScoreCalculator weights."""
    from app.core.scoring import BayesianScoreCalculator

    defaults = BayesianScoreCalculator.DEFAULT_WEIGHTS
    return {k: calibration.get(k, defaults.get(k, 0.0)) for k in defaults}


async def invalidate_calibration_cache() -> int:
    """Инвалидировать все calibration кэши."""
    count = await cache_delete_pattern("calibration:*")
    logger.info("calibration_cache_invalidated", count=count)
    return count
