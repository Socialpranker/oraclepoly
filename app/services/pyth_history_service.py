"""Pyth Hermes REST history fetcher с Redis-кэшем.

Назначение:
    D45 detector нуждается в price_at_window_open для расчёта pyth_move_pct.
    Live SSE stream (`pyth_stream_service.py`) даёт price_now, но не исторический
    snapshot. Hermes REST endpoint `/v2/updates/price/<timestamp>` возвращает
    price-update первого slot ≥ requested timestamp.

Ключевые свойства:
    - Rate limit Pyth Hermes: ~10 req/10s/IP на REST. Мы кэшируем результат
      по (symbol, window_start_ts) в Redis на ~25 часов (TTL > горизонт 4h
      window + buffer для resolve job). При cache hit — 0 запросов.
    - Cache key: `pyth:hist:{symbol}:{ts}`. Значение — `{"price": float}`
      (publish_time не храним, он не нужен downstream).
    - Если REST упал / нет данных — возвращаем None. Caller (Stage 2 cron)
      просто скипает market этот тик и попробует через 5s.
    - Pyth REST принимает unix timestamp в секундах (verified в docs).

Production-ready ноты:
    - Один глобальный httpx.AsyncClient переиспользуется между вызовами
      (singleton). Connection pooling экономит ~30ms на handshake.
    - Timeout 5s — короткий, потому что мы в hot loop (5-секундный scan).
      Лучше скипнуть тик, чем заблокировать batch.
    - Все ошибки логируются на `warning` уровне, не raise — каскадные
      падения недопустимы.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from app.core.cache import cache_get, cache_set
from app.services.pyth_stream_service import FEEDS

logger = structlog.get_logger(__name__)

HERMES_HISTORY_URL = "https://hermes.pyth.network/v2/updates/price/{ts}"
CACHE_TTL_SEC = 90_000  # ~25 часов — переживает 4h window + resolve buffer
HTTP_TIMEOUT_S = 5.0
CACHE_NAMESPACE = "pyth:hist"


_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    """Lazy singleton httpx client с connection pooling.

    Lock защищает от race при первом обращении (несколько concurrent
    задач cron могут сюда попасть одновременно).
    """
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
    return _client


async def close_client() -> None:
    """Закрыть singleton client. Вызывать из app lifespan shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _cache_key(symbol: str, window_start_ts: int) -> str:
    return f"{CACHE_NAMESPACE}:{symbol}:{window_start_ts}"


def _parse_response(payload: dict[str, Any], symbol: str) -> float | None:
    """Извлечь price для symbol из Hermes response.

    Hermes возвращает {"parsed": [{"id": "...", "price": {"price": "...", "expo": int, ...}}]}.
    Сравниваем id с нашим feed (без 0x prefix как у Pyth).
    """
    feed_id_hex = FEEDS.get(symbol)
    if feed_id_hex is None:
        return None
    expected = feed_id_hex.removeprefix("0x").lower()

    parsed = payload.get("parsed")
    if not isinstance(parsed, list):
        return None

    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str):
            continue
        if entry_id.removeprefix("0x").lower() != expected:
            continue
        price_block = entry.get("price")
        if not isinstance(price_block, dict):
            continue
        try:
            mantissa = int(price_block["price"])
            expo = int(price_block["expo"])
        except (KeyError, ValueError, TypeError):
            return None
        return mantissa * (10.0**expo)
    return None


async def get_price_at(symbol: str, unix_ts: int) -> float | None:
    """Получить Pyth price для symbol в момент unix_ts (seconds, UTC).

    Args:
        symbol: 'BTC/USD' | 'ETH/USD' | 'SOL/USD' | 'XRP/USD'.
        unix_ts: timestamp в секундах. Hermes найдёт первый slot ≥ ts.

    Returns:
        Price (float) или None если symbol неизвестен, REST упал,
        либо Hermes не отдал данных за этот timestamp.

    Кэшируется в Redis по (symbol, unix_ts) на CACHE_TTL_SEC.
    """
    if symbol not in FEEDS:
        return None

    key = _cache_key(symbol, unix_ts)
    cached = await cache_get(key)
    if isinstance(cached, dict) and "price" in cached:
        try:
            return float(cached["price"])
        except (TypeError, ValueError):
            pass  # corrupted cache → refetch

    feed_id = FEEDS[symbol]
    url = HERMES_HISTORY_URL.format(ts=unix_ts)
    params = [("ids[]", feed_id), ("parsed", "true")]

    try:
        client = await _get_client()
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "pyth_history_fetch_failed",
            symbol=symbol,
            unix_ts=unix_ts,
            error=str(exc),
        )
        return None

    price = _parse_response(payload, symbol)
    if price is None:
        logger.warning(
            "pyth_history_parse_failed",
            symbol=symbol,
            unix_ts=unix_ts,
            payload_keys=list(payload.keys()) if isinstance(payload, dict) else None,
        )
        return None

    await cache_set(key, {"price": price}, ttl_seconds=CACHE_TTL_SEC)
    return price
