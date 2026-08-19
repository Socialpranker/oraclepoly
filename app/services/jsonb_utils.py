"""JSONB helpers для asyncpg rows.

asyncpg по умолчанию (без `set_type_codec`) возвращает JSONB-колонки как str.
Эти helpers нормализуют значения в Python dict, безопасно обрабатывая
None / empty / невалидный JSON.

Использование:

    from app.services.jsonb_utils import parse_jsonb

    fee_schedule = parse_jsonb(row["fee_schedule"])  # → dict | None
"""

from __future__ import annotations

import json
from typing import Any


def parse_jsonb(raw: Any) -> dict | None:
    """Распарсить JSONB значение из asyncpg row в dict.

    Args:
        raw: значение из row[col]. Может быть None, dict (если codec настроен),
             str (default asyncpg behaviour), либо что-то ещё.

    Returns:
        dict если raw — валидный JSON object, иначе None. Списки/строки/числа
        внутри JSON возвращают None (мы храним только object-shape).
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None
    return None
