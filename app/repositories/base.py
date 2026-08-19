"""
Base Repository — общие CRUD-паттерны для всех репозиториев.

Предоставляет метод `_build_insert` для построения INSERT-запроса
из словаря данных, и `_create` для выполнения вставки.
"""

from __future__ import annotations

from typing import Any

from app.services.database import fetchrow


class BaseRepository:
    """Базовый репозиторий с общими CRUD-паттернами."""

    async def _create(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        """Создать запись в таблице и вернуть её."""
        query, values = self._build_insert(table, data)
        row = await fetchrow(query, *values)
        return dict(row) if row else {}

    def _build_insert(self, table: str, data: dict[str, Any]) -> tuple[str, list[Any]]:
        """Построить INSERT-запрос из словаря данных."""
        columns = list(data.keys())
        placeholders = [f"${i + 1}" for i in range(len(columns))]
        values = [data[c] for c in columns]
        query = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)}) RETURNING *"
        return query, values
