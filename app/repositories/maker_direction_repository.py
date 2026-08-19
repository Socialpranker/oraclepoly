"""
Maker Direction Repository — CRUD для maker_direction_cache.

Live snapshot maker-direction из CLOB /book. TTL 2h, обновляется
clob-book-sync hourly job. Читается oracle_calculator.

asyncpg-based, использует глобальные функции из app.services.database.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.services.database import execute, fetch, fetchrow, record_to_dict, records_to_list


class MakerDirectionRepository:
    """CRUD для таблицы maker_direction_cache."""

    async def upsert(
        self,
        market_id: UUID,
        condition_id: str,
        maker_direction_ratio: float,
        depth_imbalance: float | None,
        bid_depth_usd: float | None,
        ask_depth_usd: float | None,
        maker_count: int,
    ) -> None:
        """Upsert snapshot maker-direction для рынка."""
        await execute(
            """
            INSERT INTO maker_direction_cache
                (market_id, condition_id, maker_direction_ratio, depth_imbalance,
                 bid_depth_usd, ask_depth_usd, maker_count, fetched_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (market_id) DO UPDATE SET
                condition_id          = EXCLUDED.condition_id,
                maker_direction_ratio = EXCLUDED.maker_direction_ratio,
                depth_imbalance       = EXCLUDED.depth_imbalance,
                bid_depth_usd         = EXCLUDED.bid_depth_usd,
                ask_depth_usd         = EXCLUDED.ask_depth_usd,
                maker_count           = EXCLUDED.maker_count,
                fetched_at            = NOW()
            """,
            market_id,
            condition_id,
            maker_direction_ratio,
            depth_imbalance,
            bid_depth_usd,
            ask_depth_usd,
            maker_count,
        )

    async def get_by_market_id(self, market_id: UUID) -> dict[str, Any] | None:
        """Получить snapshot для конкретного рынка."""
        row = await fetchrow(
            "SELECT * FROM maker_direction_cache WHERE market_id = $1",
            market_id,
        )
        return record_to_dict(row)

    async def get_stale(self, ttl_hours: int = 2) -> list[dict[str, Any]]:
        """Получить записи старше TTL — кандидаты на пересинхронизацию."""
        rows = await fetch(
            """
            SELECT market_id, condition_id, maker_direction_ratio, fetched_at
            FROM maker_direction_cache
            WHERE fetched_at < NOW() - ($1 || ' hours')::INTERVAL
            """,
            str(ttl_hours),
        )
        return records_to_list(rows)

    async def get_all_active(self) -> list[dict[str, Any]]:
        """Все свежие записи (< 2h) — для oracle_calculator."""
        rows = await fetch(
            """
            SELECT market_id, condition_id, maker_direction_ratio,
                   depth_imbalance, maker_count, fetched_at
            FROM maker_direction_cache
            WHERE fetched_at > NOW() - INTERVAL '2 hours'
            """
        )
        return records_to_list(rows)
