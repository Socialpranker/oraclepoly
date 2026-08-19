"""
Event Repository — data access layer for events table.

asyncpg-based, uses global connection pool from app.services.database.
"""

from collections import defaultdict
from typing import Any

from app.services.database import execute, fetch, fetchrow, record_to_dict, records_to_list

# Колонки маркетов для листинга в events (без тяжёлых JSONB)
_MARKET_LIST_COLUMNS = """
    id, polymarket_id, condition_id, question, category, icon_url,
    current_price, oracle_score, previous_oracle_score,
    sentiment_score, whale_confidence, risk_score,
    volume_24h, end_date, is_active, horizon, event_id, slug,
    created_at, updated_at
""".strip()


class EventRepository:
    def __init__(self) -> None:
        pass

    async def upsert_event(self, event_data: dict[str, Any]) -> dict[str, Any]:
        """Upsert an event record. Returns the upserted row."""
        columns = list(event_data.keys())
        placeholders = [f"${i + 1}" for i in range(len(columns))]
        values = [event_data[c] for c in columns]

        # Все колонки кроме polymarket_id обновляются при конфликте
        update_cols = [c for c in columns if c != "polymarket_id"]
        update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

        query = f"""
            INSERT INTO events ({", ".join(columns)})
            VALUES ({", ".join(placeholders)})
            ON CONFLICT (polymarket_id) DO UPDATE SET {update_set}
            RETURNING *
        """
        row = await fetchrow(query, *values)
        return record_to_dict(row) or {}

    async def get_active_events(
        self,
        category: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch active events with their markets (2 queries instead of N+1)."""
        if category and category not in ("All", "Trending"):
            rows = await fetch(
                """
                SELECT * FROM events
                WHERE is_active = true AND category = $1
                ORDER BY volume_24h DESC
                LIMIT $2
                """,
                category,
                limit,
            )
        else:
            rows = await fetch(
                """
                SELECT * FROM events
                WHERE is_active = true
                ORDER BY volume_24h DESC
                LIMIT $1
                """,
                limit,
            )

        events = records_to_list(rows)
        if not events:
            return []

        # Один запрос на все markets вместо N отдельных (без тяжёлых JSONB)
        event_ids = [e["id"] for e in events]
        market_rows = await fetch(
            f"""
            SELECT {_MARKET_LIST_COLUMNS}
            FROM markets
            WHERE event_id = ANY($1) AND is_active = true
            ORDER BY volume_24h DESC
            """,
            event_ids,
        )
        markets = records_to_list(market_rows)

        # Группировка markets по event_id
        markets_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for m in markets:
            eid = m.get("event_id")
            if eid:
                markets_by_event[str(eid)].append(m)

        for event in events:
            event["markets"] = markets_by_event.get(str(event["id"]), [])

        return events

    async def get_event_by_slug(self, slug: str) -> dict[str, Any] | None:
        """Fetch a single event by slug."""
        row = await fetchrow(
            "SELECT * FROM events WHERE slug = $1",
            slug,
        )
        return record_to_dict(row)

    async def deactivate_stale_events(self, seen_polymarket_ids: list[str]) -> int:
        """Mark events not in seen_polymarket_ids as inactive.

        Returns count of deactivated events.
        """
        if not seen_polymarket_ids:
            return 0

        # Деактивируем все активные события, чьих polymarket_id нет в списке
        result = await execute(
            """
            UPDATE events
            SET is_active = false
            WHERE is_active = true
              AND polymarket_id != ALL($1)
            """,
            seen_polymarket_ids,
        )
        # execute возвращает строку вида "UPDATE N"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0
