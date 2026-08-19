"""Token ID resolver — side-aware extraction из markets.clob_token_ids JSONB.

Polymarket CLOB markets имеют 2 token outcomes per market: YES + NO.
Каждый token имеет свой `token_id` (ERC-1155 token id) для CLOB ордеров.

Schema (markets.clob_token_ids):
    JSONB array of 2 strings: [yes_token_id, no_token_id]
    Per market_sync.py:307-323 — Yes ВСЕГДА index 0, No index 1.

Use case:
    Detector emit'ит signal с side ('YES'|'NO') — нужен соответствующий
    token_id для последующего CLOB полла (v2_execution.py:196 early-skips
    signals без token_id).

Note on asyncpg JSONB: asyncpg returns JSONB columns as raw JSON string by
default (not auto-decoded to Python list). Helper handles both list (decoded)
and str (raw JSONB) inputs.
"""

from __future__ import annotations

import json
from typing import Any


def _normalize(clob_token_ids: Any) -> list | None:
    """Normalize input to list. asyncpg may return JSONB as JSON string."""
    if clob_token_ids is None:
        return None
    if isinstance(clob_token_ids, list):
        return clob_token_ids
    if isinstance(clob_token_ids, str):
        try:
            parsed = json.loads(clob_token_ids)
        except (ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def resolve_token_id(clob_token_ids: Any, side: str) -> str | None:
    """Вернуть token_id для side ('YES' или 'NO').

    Args:
        clob_token_ids: list[str] длины 2 ([yes_id, no_id]) или JSONB-string
            от asyncpg (`'["yes_id","no_id"]'`). None возвращает None.
        side: 'YES' или 'NO' (case-insensitive).

    Returns:
        Соответствующий token_id или None если clob_token_ids пустой/невалидный.
    """
    arr = _normalize(clob_token_ids)
    if not arr or len(arr) < 2:
        return None

    side_norm = side.strip().upper()
    if side_norm == "YES":
        token = arr[0]
    elif side_norm == "NO":
        token = arr[1]
    else:
        return None

    if token is None:
        return None
    return str(token)


def yes_token_id(clob_token_ids: Any) -> str | None:
    """Shortcut для YES side (D36 obi_pressure book-fetch; YES-side detectors)."""
    return resolve_token_id(clob_token_ids, "YES")


def no_token_id(clob_token_ids: Any) -> str | None:
    """Shortcut для NO side (used in D5/D7b/D10b/D13/D14/D_esports)."""
    return resolve_token_id(clob_token_ids, "NO")
