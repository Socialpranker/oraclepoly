"""
Oracle Backend - Polymarket Service

Handles discovery via Gamma API and trading via CLOB Client.
Includes Circuit Breaker pattern and retry logic for resilience.
"""

from typing import Any

import httpx
import pybreaker
import structlog
from py_clob_client_v2 import ClobClient

from app.core.circuit_breaker import polymarket_breaker
from app.core.config import get_settings
from app.core.retry import http_retry

logger = structlog.get_logger()


class PolymarketService:
    """Service to interact with Polymarket Gamma and CLOB APIs."""

    def __init__(self):
        self.settings = get_settings()
        self.gamma_url = "https://gamma-api.polymarket.com"
        self._clob_client: ClobClient | None = None

    @property
    def clob_client(self) -> ClobClient:
        """Lazy-loaded PolyMarket CLOB Client.

        Канонический путь: app.services.wallet_resolver.build_clob_client().
        signature_type/funder читаются из settings.polymarket_signature_type / polymarket_address.
        """
        if self._clob_client is None:
            from app.services.wallet_resolver import build_clob_client

            self._clob_client = build_clob_client()
        return self._clob_client

    async def fetch_active_markets(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        Fetch active markets from Gamma API.

        Protected by Circuit Breaker - returns empty list if circuit is OPEN.

        Args:
            limit: Number of markets to fetch.

        Returns:
            List of market dictionaries.
        """
        try:
            markets = await self._fetch_markets_with_resilience(limit)
            logger.info("polymarket_fetch_ok", count=len(markets))
            return markets
        except pybreaker.CircuitBreakerError:
            logger.error("polymarket_circuit_open", msg="Circuit breaker is OPEN, returning empty data")
            return []
        except Exception as e:
            logger.error("polymarket_fetch_error", error=str(e), error_type=type(e).__name__)
            return []

    @polymarket_breaker
    @http_retry
    async def _fetch_markets_with_resilience(self, limit: int) -> list[dict[str, Any]]:
        """
        Internal method with Circuit Breaker and Retry.

        Retry happens INSIDE Circuit Breaker to count final failures correctly.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"active": "true", "closed": "false", "limit": limit, "order": "volume24hr", "ascending": "false"}
            response = await client.get(f"{self.gamma_url}/markets", params=params)
            response.raise_for_status()
            return response.json()

    async def get_market_details(self, condition_id: str) -> dict[str, Any]:
        """
        Fetch detailed info for a specific market.

        Protected by Circuit Breaker and Retry.
        """
        try:
            return await self._fetch_market_details_with_resilience(condition_id)
        except pybreaker.CircuitBreakerError:
            logger.error("polymarket_circuit_open", condition_id=condition_id)
            return {}
        except Exception as e:
            logger.error("polymarket_detail_error", condition_id=condition_id, error=str(e))
            return {}

    @polymarket_breaker
    @http_retry
    async def _fetch_market_details_with_resilience(self, condition_id: str) -> dict[str, Any]:
        """Internal method with Circuit Breaker and Retry."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self.gamma_url}/markets",
                params={"conditionId": condition_id},
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return data[0] if data else {}
            return data

    async def fetch_active_events(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Fetch active events from Gamma API with nested markets.

        Args:
            limit: Max events per page (max 100).
            offset: Pagination offset (0, 100, 200...).

        Returns:
            List of event dicts with nested markets[].
        """
        try:
            return await self._fetch_events_with_resilience(limit=limit, offset=offset)
        except Exception as e:
            logger.error("fetch_events_failed", error=str(e), offset=offset)
            return []

    async def fetch_closed_events(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Fetch recently closed events from Gamma API.

        Нужно для обновления current_price закрытых рынков (outcomePrices → 0 или 1),
        чтобы resolve_outcomes мог определить исход.

        Args:
            limit: Max events per page (max 100).
            offset: Pagination offset.

        Returns:
            List of closed event dicts with nested markets[].
        """
        try:
            return await self._fetch_closed_events_with_resilience(limit=limit, offset=offset)
        except Exception as e:
            logger.error("fetch_closed_events_failed", error=str(e), offset=offset)
            return []

    @polymarket_breaker
    @http_retry
    async def _fetch_events_with_resilience(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Resilient event fetch with circuit breaker and retry."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self.gamma_url}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                    "limit": limit,
                    "offset": offset,
                },
            )
            response.raise_for_status()
            return response.json()

    @polymarket_breaker
    @http_retry
    async def _fetch_closed_events_with_resilience(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Resilient closed event fetch with circuit breaker and retry."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self.gamma_url}/events",
                params={
                    "closed": "true",
                    "order": "endDate",
                    "ascending": "false",
                    "limit": limit,
                    "offset": offset,
                },
            )
            response.raise_for_status()
            return response.json()


def get_polymarket_service() -> PolymarketService:
    """Helper to get PolymarketService instance."""
    return PolymarketService()
