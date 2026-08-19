"""Pagination helper для Polymarket data-api /trades.

`/trades?market=<cid>&before=<unix_ts>` отдаёт trades с
`timestamp <= before` (inclusive). Если выставлять `before = min_ts`
очередной страницы, cursor не двигается на границах с одинаковыми
timestamp'ами — следующий запрос возвращает те же trades, цикл крутится
до runaway-cap. Это reproducible на низколиквидных markets с тиков
одинакового timestamp.

Инцидент 2026-05-24: live_trade_bucket_writer + live_wallet_flow_writer
выжимали ~50K избыточных запросов в час → 939 HTTP 429/час.

Фикс: `before = min_ts - 1` (exclusive) + safety break, если cursor
не сдвинулся между итерациями.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.data_api_limiter import acquire_data_api_slot
from app.core.retry import http_retry

POLYMARKET_TRADES_URL = "https://data-api.polymarket.com/trades"
DEFAULT_LIMIT = 500
DEFAULT_MAX_PAGES = 20
DEFAULT_TIMEOUT = 15.0


@http_retry
async def _fetch_one_page(
    client: httpx.AsyncClient,
    params: dict[str, Any],
    timeout: float,
) -> list[dict[str, Any]]:
    """Single page fetch с retry на 429/5xx (3 attempts, exp backoff 2→4→8s)."""
    await acquire_data_api_slot()
    resp = await client.get(POLYMARKET_TRADES_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json() or []


async def fetch_trades_since(
    client: httpx.AsyncClient,
    condition_id: str,
    since_ts: int,
    *,
    limit: int = DEFAULT_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Fetch trades for one market starting from `since_ts` (unix seconds).

    Polymarket data-api возвращает trades по убыванию `timestamp`. Мы листаем
    через `before=<ts>` пока не достигли since_ts, пустого ответа, runaway cap'а
    или зацикливания на boundary timestamp.

    Returns: trades с `timestamp > since_ts`. Caller сам filter'ит дальше.
    """
    out: list[dict[str, Any]] = []
    before: int | None = None

    for _page in range(max_pages):
        params: dict[str, Any] = {"market": condition_id, "limit": limit}
        if before is not None:
            params["before"] = before

        trades = await _fetch_one_page(client, params, timeout)
        if not trades:
            break

        fresh = [t for t in trades if int(t.get("timestamp", 0)) > since_ts]
        out.extend(fresh)

        # Дошли до since_ts: на этой странице уже есть trades старее → выходим.
        if len(fresh) < len(trades):
            break

        try:
            min_ts = min(int(t.get("timestamp", 0)) for t in trades)
        except ValueError:
            break
        if min_ts <= since_ts:
            break

        # `before=ts` для data-api inclusive (возвращает trades с timestamp <= ts).
        # Чтобы cursor двигался, выставляем `before = min_ts - 1`. Иначе при
        # одинаковых timestamps подряд cursor застревает → infinite loop до
        # runaway cap'а → ~20 redundant requests на market.
        next_before = min_ts - 1
        if before is not None and next_before >= before:
            # Safety: cursor не сдвинулся вперёд (или поехал назад) — bail.
            break
        before = next_before

    return out
