"""
Reconcile Orphan Transactions — привязка market_id к транзакциям без рынка.

Транзакции приходят из trade-poll (каждые 5 мин), но market_id может быть NULL
если рынок ещё не синхронизирован (market-sync каждые 4ч).
Этот job пытается привязать orphan-транзакции по market_slug.
Старые нерешаемые orphans (>7 дней, slug без matching market) удаляются автоматически.

Запуск: python scripts/cron_runner.py reconcile-orphans
"""

from typing import Any

import structlog

from app.services.database import execute, fetch

logger = structlog.get_logger(__name__)

# Orphans старше этого срока с несуществующим market удаляются
ORPHAN_TTL_DAYS = 7


async def run_reconcile_orphans() -> dict[str, Any]:
    """Привязать market_id к orphan-транзакциям по market_slug + очистка старых."""
    # ── 1. Reconcile: slug → market_id ──
    orphans = await fetch(
        """
        SELECT DISTINCT t.market_slug
        FROM transactions t
        WHERE t.market_id IS NULL
          AND t.market_slug IS NOT NULL
          AND t.market_slug != ''
          AND EXISTS (SELECT 1 FROM markets m WHERE m.slug = t.market_slug)
        """
    )

    reconciled = 0
    for row in orphans:
        slug = row["market_slug"]
        result = await execute(
            """
            UPDATE transactions t
            SET market_id = m.id
            FROM markets m
            WHERE t.market_id IS NULL
              AND t.market_slug = $1
              AND m.slug = $1
            """,
            slug,
        )
        if result and isinstance(result, str):
            count = int(result.split()[-1]) if result.startswith("UPDATE") else 0
            reconciled += count

    # ── 2. Cleanup: удалить старые нерешаемые orphans ──
    cleanup_result = await execute(
        f"""
        DELETE FROM transactions t
        WHERE t.market_id IS NULL
          AND t.created_at < NOW() - INTERVAL '{ORPHAN_TTL_DAYS} days'
          AND NOT EXISTS (
              SELECT 1 FROM markets m WHERE m.slug = t.market_slug
          )
        """,
    )
    cleaned = 0
    if cleanup_result and isinstance(cleanup_result, str):
        cleaned = int(cleanup_result.split()[-1]) if cleanup_result.startswith("DELETE") else 0

    # ── 3. Подсчёт оставшихся ──
    remaining_row = await fetch("SELECT COUNT(*) as cnt FROM transactions WHERE market_id IS NULL")
    remaining = remaining_row[0]["cnt"] if remaining_row else 0

    logger.info(
        "reconcile_orphans_done",
        slugs_checked=len(orphans),
        reconciled=reconciled,
        cleaned=cleaned,
        remaining=remaining,
    )

    return {
        "status": "ok",
        "slugs_checked": len(orphans),
        "reconciled": reconciled,
        "cleaned": cleaned,
        "remaining": remaining,
    }
