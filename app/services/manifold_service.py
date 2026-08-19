"""Manifold Markets REST client — public API, no auth.

PDF §11 information edge source: "Топовый источник information edge —
early signals до миграции в Polymarket".

Manifold = play-money + small cash prediction market platform. Markets
mostly retail belief; divergence vs Polymarket indicates either:
- Manifold retail wrong (Polymarket smart money correct), OR
- Manifold early signal — Polymarket will follow within hours/days

Used by:
- Phase 1: manifold-scrape cron — ingest active markets + snapshots
- Phase 2: matcher → canonical_events through embedding similarity
- Phase 3: cross-venue divergence detector

Endpoints (https://docs.manifold.markets/api):
- GET /v0/markets?limit=N&before=ID  — paginated list, sorted by createdTime desc
- GET /v0/market/{id}                — full market detail
- GET /v0/slug/{slug}                — find by slug

No authentication required for read endpoints.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.core.retry import http_retry

logger = structlog.get_logger()


MANIFOLD_API_BASE = "https://api.manifold.markets/v0"
DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_PAGE_SIZE = 1000  # Manifold max per request


class ManifoldClient:
    """Read-only Manifold REST client.

    Не используется POLYMARKET_PROXY — это public read-only API
    (соответствует CLAUDE.md rule про proxy execution-only).
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_SEC):
        self.base_url = MANIFOLD_API_BASE
        self.timeout = timeout

    @http_retry
    async def list_markets(
        self,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /v0/markets — список markets.

        Args:
            limit: max 1000 per request (Manifold cap).
            before: pagination cursor — market id из last page.

        Returns:
            List of market dicts (lite format — without bets/comments).
        """
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/markets", params=params)
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, list):
            logger.warning("manifold_list_markets_unexpected_shape", data_type=type(data).__name__)
            return []
        return data

    @http_retry
    async def get_market(self, market_id: str) -> dict[str, Any] | None:
        """GET /v0/market/{id} — full market detail."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/market/{market_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    @http_retry
    async def get_by_slug(self, slug: str) -> dict[str, Any] | None:
        """GET /v0/slug/{slug} — find by slug."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/slug/{slug}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    @http_retry
    async def list_bets_for_market(
        self,
        market_id: str,
        *,
        limit: int = 1000,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /v0/bets?contractId=X — individual bet history.

        Возвращает bets отсортированные по createdTime desc.
        Pagination через `before` cursor (bet id).

        Args:
            market_id: Manifold contract/market id.
            limit: max 1000 per request (Manifold cap).
            before: pagination cursor — bet id из предыдущей страницы.

        Returns:
            List of bet dicts. Каждый bet содержит: id, userId, amount, outcome,
            probBefore, probAfter, shares, isFilled, isRedemption, fees, createdTime.
        """
        params: dict[str, Any] = {"contractId": market_id, "limit": limit}
        if before:
            params["before"] = before

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/bets", params=params)
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, list):
            logger.warning(
                "manifold_list_bets_unexpected_shape",
                market_id=market_id,
                data_type=type(data).__name__,
            )
            return []
        return data

    async def list_bets_since(
        self,
        market_id: str,
        *,
        since_bet_id: str | None = None,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Pull bets newer чем since_bet_id (incremental fetch).

        Manifold API не поддерживает фильтр "after timestamp", только
        cursor-based "before id" (старые направления). Стратегия: pull page,
        stop когда увидим since_bet_id или max_pages.

        Args:
            market_id: contractId.
            since_bet_id: stop когда видим этот bet (он уже у нас в БД).
            max_pages: safety cap (10 × 1000 = 10K bets per run).
        """
        result: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            page = await self.list_bets_for_market(market_id, limit=DEFAULT_PAGE_SIZE, before=cursor)
            if not page:
                break

            stop = False
            for bet in page:
                if since_bet_id and bet.get("id") == since_bet_id:
                    stop = True
                    break
                result.append(bet)
            if stop or len(page) < DEFAULT_PAGE_SIZE:
                break
            cursor = page[-1]["id"]
        return result

    async def list_active_binary_markets(
        self,
        *,
        max_pages: int = 5,
        min_volume_24h: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Aggregate over pagination, filter to BINARY non-resolved.

        Phase 1: только BINARY markets (мапит на Polymarket YES/NO).
        Manifold MULTIPLE_CHOICE / PSEUDO_NUMERIC — Phase 2.

        Args:
            max_pages: pagination safety cap (5 × 1000 = 5K markets max).
            min_volume_24h: filter low-volume noise.
        """
        result: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            page = await self.list_markets(limit=DEFAULT_PAGE_SIZE, before=cursor)
            if not page:
                break
            for m in page:
                if m.get("outcomeType") != "BINARY":
                    continue
                if m.get("isResolved"):
                    continue
                if float(m.get("volume24Hours") or 0) < min_volume_24h:
                    continue
                result.append(m)
            cursor = page[-1].get("id")
            if len(page) < DEFAULT_PAGE_SIZE:
                break  # last page

        logger.info("manifold_list_active_binary_done", count=len(result))
        return result


def get_manifold_client() -> ManifoldClient:
    return ManifoldClient()
