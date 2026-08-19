"""Polymarket Data API /activity client.

В отличие от /trades (который ограничен offset=3000 и не поддерживает
timestamp фильтрацию), /activity поддерживает start/end (unix seconds)
и позволяет глубокий бэкфилл по любому периоду.

Используется для исторического backfill detector_signals.

Response shape (TRADE):
    {
        "proxyWallet": "0x...",
        "timestamp": 1723772457,           # unix seconds
        "conditionId": "0x...",
        "type": "TRADE",
        "size": 600,                       # shares
        "usdcSize": 354,                   # = size * price (USD value)
        "transactionHash": "0x...",
        "price": 0.59,
        "side": "BUY",                     # BUY | SELL
        "title": "...",
        "outcome": "No"                    # Yes | No
    }
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pybreaker
import structlog

from app.core.circuit_breaker import polymarket_breaker
from app.core.retry import http_retry

logger = structlog.get_logger()

ACTIVITY_API_URL = "https://data-api.polymarket.com/activity"
DEFAULT_LIMIT = 500


class PolymarketActivityService:
    """Read-only client для /activity endpoint."""

    def __init__(self) -> None:
        self.url = ACTIVITY_API_URL

    async def fetch_activity_window(
        self,
        *,
        start: int,
        end: int,
        activity_type: str = "TRADE",
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        user: str | None = None,
        side: str | None = None,
    ) -> list[dict[str, Any]]:
        """Один батч /activity на окне [start, end] (unix seconds).

        Возвращает пустой список при ошибке (Circuit Breaker / network).
        """
        try:
            return await self._fetch_with_resilience(
                start=start,
                end=end,
                activity_type=activity_type,
                limit=limit,
                offset=offset,
                user=user,
                side=side,
            )
        except pybreaker.CircuitBreakerError:
            logger.error("activity_circuit_open", start=start, end=end)
            return []
        except Exception as exc:
            logger.error(
                "activity_fetch_error",
                start=start,
                end=end,
                offset=offset,
                error=str(exc),
            )
            return []

    @polymarket_breaker
    @http_retry
    async def _fetch_with_resilience(
        self,
        *,
        start: int,
        end: int,
        activity_type: str,
        limit: int,
        offset: int,
        user: str | None,
        side: str | None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "type": activity_type,
            "start": start,
            "end": end,
            "limit": limit,
            "offset": offset,
        }
        if user:
            params["user"] = user.lower()
        if side:
            params["side"] = side
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(self.url, params=params)
            response.raise_for_status()
            return response.json()

    async def iterate_window(
        self,
        *,
        start: int,
        end: int,
        activity_type: str = "TRADE",
        page_size: int = DEFAULT_LIMIT,
        max_pages: int | None = None,
        rate_delay: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Постранично собирает все события за окно [start, end].

        API возвращает в descending timestamp order. Idempotency на нашей
        стороне через `tx_hash` в `detector_signals`.
        """
        all_events: list[dict[str, Any]] = []
        offset = 0
        page = 0
        while True:
            batch = await self.fetch_activity_window(
                start=start,
                end=end,
                activity_type=activity_type,
                limit=page_size,
                offset=offset,
            )
            if not batch:
                break
            all_events.extend(batch)
            page += 1
            if max_pages is not None and page >= max_pages:
                break
            if len(batch) < page_size:
                break
            offset += page_size
            if rate_delay:
                await asyncio.sleep(rate_delay)
        return all_events


def get_polymarket_activity_service() -> PolymarketActivityService:
    return PolymarketActivityService()
