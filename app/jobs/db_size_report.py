"""DB Size Report — bi-weekly TG-монитор объёма БД и роста таблиц.

Месяц «честная проверка edge» = live-pipeline копит данные. Snapshot-таблицы
(detector_signal_price_snapshots ~3GB, live_*) растут быстро. Этот джоб раз в 2
недели шлёт в TG общий размер БД + топ-таблицы + предупреждение, если БД пухнет
быстрее retention. Не чистит ничего — только наблюдает (cleanup-краны режут сами).

Cron: раз в 2 недели (1 и 15 числа), лёгкий read-only.
См. strategy_edge_not_data_2026-06-15 (Поток 2 — следить за объёмом).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.analytics.notification import Notification, Severity
from app.services.database import fetch, fetchval
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

# Порог тревоги: если БД крупнее — явно пометить в отчёте (сверяться с реальным размером диска).
_DB_WARN_GB = 40.0
_TOP_N = 8

_TOP_TABLES_SQL = """
SELECT relname AS table_name,
       pg_total_relation_size(relid) AS bytes,
       n_live_tup AS rows
  FROM pg_stat_user_tables
 ORDER BY pg_total_relation_size(relid) DESC
 LIMIT $1
"""


def to_notifications(db_bytes: int, tables: list[dict[str, Any]]) -> list[Notification]:
    """Отчёт о размере БД → Notification (спека §3).

    Severity зависит от факта, а не задана константой: обычный двухнедельный
    отчёт — FYI, он идёт строкой в Daily Brief (§4, «мониторы, которые шлют по
    расписанию независимо от результата»). Но превышение _DB_WARN_GB — уже
    состояние с действием, и уводить его в дайджест значит потерять (риск №4 §10).

    what_if_ignored=None: честной цифры нет. Диск 301 ГБ, БД ~10 ГБ — «кончится
    место» это догадка, а не факт, и §3 запрещает её печатать как цифру.
    """
    db_gb = db_bytes / 1e9
    warn = db_gb >= _DB_WARN_GB

    top = ", ".join(f"{t['table_name']} {float(t['bytes']) / 1e9:.1f} ГБ" for t in tables[:3])
    lines = [f"Всего база занимает {db_gb:.2f} ГБ."]
    if top:
        lines.append(f"Крупнейшие таблицы: {top}.")
    if warn:
        lines.append(f"Это выше порога наблюдения {_DB_WARN_GB:.0f} ГБ.")

    return [
        Notification(
            severity=Severity.ACTION_REQUIRED if warn else Severity.FYI,
            subject="infra:db_size",
            kind="db_size_report",
            headline="База пухнет быстрее ожидаемого" if warn else "Размер базы за две недели",
            what_happened=lines,
            what_if_ignored=None,
            what_to_do=(
                "Проверь логи retention-кранов (cleanup, orderbook-cap) — отключать их нельзя. "
                "Если краны работают, а рост держится, порог наблюдения пора пересмотреть."
            )
            if warn
            else None,
            actions=[],
            evidence=[f"{t['table_name']}={float(t['bytes']) / 1e9:.2f}ГБ/{int(t['rows'] or 0)} строк" for t in tables],
        )
    ]


async def run_db_size_report() -> dict[str, Any]:
    """Собрать размер БД + топ-таблицы и отправить через шину уведомлений."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    db_bytes = int(await fetchval("SELECT pg_database_size(current_database())") or 0)
    rows = await fetch(_TOP_TABLES_SQL, _TOP_N)
    tables = [dict(r) for r in rows]

    published = await publish(to_notifications(db_bytes, tables), actor="db_size_report")

    logger.info(
        "db_size_report_completed",
        db_gb=round(db_bytes / 1e9, 2),
        sent=published.sent,
        digested=published.digested,
        today=today,
    )
    return {"db_bytes": db_bytes, "n_tables": len(tables), "published": published}
