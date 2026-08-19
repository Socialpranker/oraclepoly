"""
DB Size Monitor Job

Проверяет размеры ключевых таблиц и шлёт Telegram-алерт если превышен порог.
Запускается раз в сутки.
"""

import structlog

from app.analytics.notification import Notification, Severity
from app.services.database import fetch
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

# Пороги алертов в МБ. Реальные размеры в фоне:
# - whales: 208 MB (накопительный аналитический актив, ~550 MB/год рост)
# oracle_snapshots НЕ мониторим — legacy V1, пуста by-design с миграции 11.05
# (резолюция переехала на markets.winner).
#
# markets порог поднят 1024→1200 (2026-06-28): живых данных ~800 MB сразу после
# VACUUM FULL, но clear_raw_for_closed_markets делает массовый UPDATE...raw=NULL,
# оставляя TOAST/heap "дырки", которые обычный autovacuum не возвращает ОС. Размер
# на диске колеблется у 1024 → 4 ложных рецидива подряд (10/17/20/28 июня). Дырки
# теперь возвращает еженедельная vacuum-markets джоба (app.jobs.vacuum_maintenance),
# порог 1200 даёт запас над пиками между её прогонами.
ALERT_THRESHOLDS_MB: dict[str, int] = {
    "transactions": 2048,  # 2 GB
    "whales": 350,  # 350 MB (естественный рост 550 MB/год)
    "markets": 1200,  # 1.2 GB (~800 MB живых + TOAST-дырки между прогонами vacuum-markets)
}

MONITORED_TABLES = list(ALERT_THRESHOLDS_MB.keys())


def to_notifications(alerts: list[dict]) -> list[Notification]:
    """Превышения порогов → Notification (спека §3).

    Одно уведомление на все таблицы, а не по одному на таблицу: причина общая
    (retention не справляется), действие тоже одно — и три отдельных сообщения
    про три таблицы это ровно тот шум, от которого уводит §4.

    what_if_ignored=None намеренно: спека §3 запрещает выдумывать цифру.
    Честной цены бездействия здесь нет — «диск когда-нибудь кончится» это не
    факт, а догадка, а до реального заполнения диска далеко.
    """
    if not alerts:
        return []

    lines = [f"Таблица {a['table']} выросла до {a['total_mb']} МБ при пороге {a['threshold_mb']} МБ." for a in alerts]
    lines.append("Порог — это не авария, а сигнал, что чистка отстаёт от роста.")

    return [
        Notification(
            severity=Severity.ACTION_REQUIRED,
            subject="infra:db_size",
            kind="db_size_threshold",
            headline="Таблицы БД переросли порог",
            what_happened=lines,
            what_if_ignored=None,
            what_to_do=(
                "Проверь, что retention-краны работают: логи cleanup-джоб и vacuum-maintenance. "
                "Если чистка идёт, а размер держится — порог устарел и его надо поднять."
            ),
            actions=[],
            evidence=[f"{a['table']}={a['total_mb']}МБ/порог {a['threshold_mb']}МБ" for a in alerts],
        )
    ]


async def run_db_size_monitor() -> dict:
    rows = await fetch(
        """
        SELECT
            relname AS table_name,
            pg_total_relation_size(c.oid) AS total_bytes,
            pg_relation_size(c.oid) AS data_bytes,
            pg_indexes_size(c.oid) AS index_bytes
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
          AND relname = ANY($1)
        ORDER BY total_bytes DESC
        """,
        MONITORED_TABLES,
    )

    sizes: dict[str, dict] = {}
    alerts: list[dict] = []

    for row in rows:
        name = row["table_name"]
        total_mb = row["total_bytes"] / (1024 * 1024)
        data_mb = row["data_bytes"] / (1024 * 1024)
        index_mb = row["index_bytes"] / (1024 * 1024)

        sizes[name] = {
            "total_mb": round(total_mb, 1),
            "data_mb": round(data_mb, 1),
            "index_mb": round(index_mb, 1),
        }

        threshold = ALERT_THRESHOLDS_MB.get(name)
        if threshold and total_mb >= threshold:
            # Хранится структурно, а не готовой HTML-строкой: текст собирает
            # рендерер конверта, джоба отдаёт факты (спека §2 п.1).
            alerts.append({"table": name, "total_mb": round(total_mb), "threshold_mb": threshold})

    # Логируем всегда
    for name, info in sizes.items():
        logger.info(
            "db_table_size",
            table=name,
            total_mb=info["total_mb"],
            data_mb=info["data_mb"],
            index_mb=info["index_mb"],
        )

    # Telegram-алерт только если есть превышения
    published = None
    if alerts:
        published = await publish(to_notifications(alerts), actor="db_size_monitor")

    return {"sizes": sizes, "alerts": alerts, "published": published}
