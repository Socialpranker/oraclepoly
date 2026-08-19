"""Taker enrichment через Polymarket Data API.

Определяет is_taker для whale транзакций:
- Inline: resolve_is_taker() → True (found) или None (not found / error)
- Backfill: resolve_batch() → set[tx_hash] (settled данные)
"""

from __future__ import annotations

import logging

import httpx

from app.core.cache import cache_get, cache_set

logger = logging.getLogger(__name__)

DATA_API_URL = "https://data-api.polymarket.com"
CACHE_TTL = 600  # 10 мин


class TakerService:
    """Data API client для определения maker/taker."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    async def resolve_is_taker(self, address: str, condition_id: str, tx_hash: str) -> bool | None:
        """Определить is_taker для одной транзакции.

        Returns:
            True — tx_hash найден в takerOnly=true (whale = taker)
            None — не найден или ошибка (backfill разберётся)
            Никогда не возвращает False (inline не может гарантировать settled данные)
        """
        if not condition_id or not address:
            return None

        cache_key = f"taker_hashes:{address.lower()}:{condition_id}"
        cached = await cache_get(cache_key)
        if cached is not None:
            return True if tx_hash in cached else None

        taker_hashes = await self._fetch_taker_hashes(address, condition_id)
        if taker_hashes is None:
            return None

        await cache_set(cache_key, taker_hashes, ttl_seconds=CACHE_TTL)

        return True if tx_hash in taker_hashes else None

    async def resolve_batch(self, address: str, condition_id: str) -> set[str]:
        """Batch lookup: вернуть set tx_hashes где address = taker.

        Для backfill — данные settled, можно определить False.
        """
        hashes = await self._fetch_taker_hashes(address, condition_id)
        return set(hashes) if hashes else set()

    async def _fetch_taker_hashes(self, address: str, condition_id: str) -> list[str] | None:
        """Fetch tx_hashes из Data API takerOnly=true.

        Read-only Data API запрос — идёт напрямую, БЕЗ POLYMARKET_PROXY.
        Прокси разрешён только для execution_job.py (ставки).
        """
        try:
            client = self._client or httpx.AsyncClient(timeout=5.0)
            try:
                resp = await client.get(
                    f"{DATA_API_URL}/trades",
                    params={
                        "user": address.lower(),
                        "market": condition_id,
                        "takerOnly": "true",
                        "limit": 100,
                    },
                )
                resp.raise_for_status()
                trades = resp.json()
            finally:
                if not self._client:
                    await client.aclose()

            return [t.get("transactionHash", "") for t in trades if t.get("transactionHash")]

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning("taker_api_rate_limited", address=address)
            else:
                logger.debug("taker_api_error", address=address, status=e.response.status_code)
            return None
        except Exception:
            logger.debug("taker_api_fetch_failed", address=address)
            return None


_taker_service: TakerService | None = None


def get_taker_service() -> TakerService:
    """Singleton для переиспользования httpx client."""
    global _taker_service
    if _taker_service is None:
        _taker_service = TakerService()
    return _taker_service
