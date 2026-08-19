"""vacuum-markets job — weekly Sun 04:00 UTC (окно низкой активности).

Возвращает ОС место, которое освобождает clear_raw_for_closed_markets, но не
возвращает обычный autovacuum.

Проблема: cleanup ежедневно делает массовый UPDATE markets SET raw=NULL у закрытых
рынков (TOAST retention). UPDATE в Postgres = новая версия строки + мёртвая старая;
autovacuum помечает место свободным для переиспользования внутри таблицы, но НЕ
отдаёт ОС — файл на диске не сжимается. market_sync каждые 15 мин пишет raw новым
закрытым событиям → cleanup снова обнуляет → "дырки" в TOAST/heap копятся. Размер на
диске растёт линейно при стабильном объёме живых данных → DB Size Alert по markets
(рецидивы 10/17/20/28 июня 2026). Единственное, что отдаёт место ОС, — VACUUM FULL
(или pg_repack), который физически пересобирает таблицу.

VACUUM FULL берёт ACCESS EXCLUSIVE lock — на время пересборки (минуты на ~400K строк
/ 1.2 GB) встают market_sync и чтение рынков. Поэтому раз в неделю в окно низкой
активности, а не в ежедневный cleanup. Дефолтный command_timeout пула (30s) VACUUM FULL
не переживает (первый прогон 2026-07-05 упал по TimeoutError) — передаём свой таймаут.

Gate по абсолютному размеру, не по bloat-ratio: TOAST хранит данные чанками 2KB со
своим индексом, поэтому даже сразу после пересборки toast_on_disk / live_raw ≈ 1.4 —
ratio-порог давал бы ложный "bloat" на здоровой таблице. Живой объём markets ~800 MB;
запускаем VACUUM FULL только когда таблица доросла до VACUUM_TRIGGER_MB (между здоровым
размером и порогом алерта 1200) — тогда возврат дырок реально нужен.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.database import execute, fetchval

logger = structlog.get_logger()

# Порог запуска VACUUM FULL по полному размеру markets на диске.
# Здоровый размер ~800 MB (живые данные), алерт db_size_monitor — 1200 MB.
# 950 ловит накопленный bloat до того, как он пробьёт порог алерта, и не блокирует
# таблицу когда она компактна.
VACUUM_TRIGGER_MB = 950

# VACUUM FULL пересобирает heap+TOAST+индексы целиком; на текущем объёме это минуты,
# не секунды. 600s — запас ×5 от ожидаемого, но конечный: зависшая пересборка не должна
# держать ACCESS EXCLUSIVE lock бесконечно.
VACUUM_TIMEOUT_S = 600.0


async def _markets_total_mb() -> float:
    total_bytes = await fetchval("SELECT pg_total_relation_size('markets')")
    return round(int(total_bytes) / (1024 * 1024), 1)


async def run_vacuum_maintenance() -> dict[str, Any]:
    """Еженедельный VACUUM FULL markets с gate по размеру.

    Returns:
        {status, before_mb, after_mb, reclaimed_mb}
    """
    before_mb = await _markets_total_mb()

    if before_mb < VACUUM_TRIGGER_MB:
        logger.info("vacuum_markets_skipped_below_trigger", total_mb=before_mb, trigger_mb=VACUUM_TRIGGER_MB)
        return {
            "status": "skipped_below_trigger",
            "before_mb": before_mb,
            "after_mb": before_mb,
            "reclaimed_mb": 0.0,
        }

    # VACUUM FULL нельзя в транзакционном блоке; asyncpg.execute одиночной командой
    # не оборачивает в BEGIN/COMMIT, поэтому проходит. ANALYZE — освежить статистику
    # после пересборки.
    await execute("VACUUM (FULL, ANALYZE) markets", timeout=VACUUM_TIMEOUT_S)

    after_mb = await _markets_total_mb()
    reclaimed_mb = round(before_mb - after_mb, 1)

    logger.info(
        "vacuum_markets_done",
        before_mb=before_mb,
        after_mb=after_mb,
        reclaimed_mb=reclaimed_mb,
    )

    return {
        "status": "vacuumed",
        "before_mb": before_mb,
        "after_mb": after_mb,
        "reclaimed_mb": reclaimed_mb,
    }
