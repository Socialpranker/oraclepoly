"""Category health — cron (hourly), материализует blacklist-кандидатов в Redis.

Динамическая замена статического _CATEGORY_BLACKLIST (v2_execution.py): ячейка
detector × category становится кандидатом на блэклист по event-кластеризованной
статистике (view event_performance, миграция 20260707093221), а не по разовой
эмпирике в комментарии.

Правило (все условия сразу — защита от выбросов-концентраторов, аудит 2026-07-07:
весь плюс Crypto делали 2 BTC-рынка):
  events >= 30           — достаточно НЕЗАВИСИМЫХ наблюдений (событий, не ордеров)
  roi < 0                — агрегатный ROI ячейки отрицателен
  median_event_roi < 0   — И типичное событие тоже убыточно

Сам гейт НЕ блокирует: v2_execution._process_signal только пишет would-block в
gate_shadow_log (shadow-паттерн D3 Sports-блэклиста). Денежная контрфактика —
в virtual_weekly_digest, включение реального блэклиста — human decision.

Fail-open by design: нет ключа в Redis / Redis недоступен → shadow-лог молчит,
money-path не затронут.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.core.cache import get_redis
from app.services.database import fetch

logger = structlog.get_logger(__name__)

# Ключ читает v2_execution._category_health_candidate на каждом сигнале.
CANDIDATES_KEY = "category_health:candidates"
# Refresh hourly; TTL 4h — переживает 2-3 пропущенных прогона, потом fail-open.
_TTL_SEC = 4 * 3600

_MIN_EVENTS = 30
_WINDOW = "60 days"

# Health считаем по virtual: live-выборка ничтожна (n<10), а virtual-поток — та же
# сигнальная логика без исполнения.
_CANDIDATES_SQL = f"""
SELECT detector, category, events, roi, median_event_roi
  FROM category_performance
 WHERE mode = 'virtual'
   AND events >= {_MIN_EVENTS}
   AND roi < 0
   AND median_event_roi < 0
"""


async def run_category_health() -> dict[str, Any]:
    rows = await fetch(_CANDIDATES_SQL)

    candidates = {
        f"{row['detector']}:{row['category']}": {
            "events": row["events"],
            "roi": round(float(row["roi"]), 4),
            "median_event_roi": round(float(row["median_event_roi"]), 4),
        }
        for row in rows
    }

    redis = await get_redis()
    if redis is None:
        logger.warning("category_health_redis_unavailable", candidates=len(candidates))
        return {"status": "error", "reason": "redis_unavailable", "candidates": len(candidates)}

    await redis.set(CANDIDATES_KEY, json.dumps(candidates), ex=_TTL_SEC)
    logger.info("category_health_refreshed", candidates=len(candidates), cells=sorted(candidates))
    return {"status": "ok", "candidates": len(candidates), "cells": sorted(candidates)}
