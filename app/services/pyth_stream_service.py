"""Pyth Hermes SSE stream — публичный feed BTC/ETH/SOL/XRP USD prices.

Используется детектором D45 (lead-lag CLOB ↔ Chainlink) как proxy для
Chainlink Data Streams (Polymarket резолвит crypto-up/down markets по нему,
но Chainlink Data Streams не self-serve — Pyth Hermes 400ms cadence работает
как close enough replacement).

Архитектура:
    PythStreamService держит длинный SSE connection и in-memory cache последних
    цен per symbol. Consumer'ы (D45 cron) читают snapshot через get_latest()
    без блокировки.

    Cache invalidation: если symbol не обновился >`stale_after_s` (default 30s),
    get_latest() вернёт None — детектор знает что данных нет.

Реальные параметры (verified 2026-05-21 prod smoke):
    Cadence ~440ms / 2.28 ticks/s per symbol.
    Publish-to-recv lag ~1-2 секунды.
    SSE auto-close через 24h → нужен reconnect (встроен в _stream_loop).

См. research/gamma-api-2026-05/chainlink_alternative.md
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

HERMES_URL = "https://hermes.pyth.network/v2/updates/price/stream"

# Pyth feed IDs (verified 2026-05-21). Mapping символа на 0x-hex.
# Полный реестр: https://www.pyth.network/developers/price-feed-ids
FEEDS: dict[str, str] = {
    "BTC/USD": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "ETH/USD": "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "SOL/USD": "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "XRP/USD": "0xec5d399846a9209f3fe5881d70aae9268c94339ff9817e8d18ff19fa05eea1c8",
}

# Pyth feed id → symbol (для парсинга incoming messages).
_FEED_TO_SYMBOL: dict[str, str] = {v: k for k, v in FEEDS.items()}


@dataclass(frozen=True)
class PythTick:
    """Snapshot одной цены."""

    symbol: str
    price: float
    publish_time_ms: int  # Pyth publish_time × 1000 (Pyth даёт сек → переводим в мс)
    received_at_ms: int  # local recv timestamp в мс
    confidence: float | None = None  # Pyth confidence interval (опционально)


class PythStreamService:
    """Async SSE consumer от Hermes. Держит in-memory cache последних tick'ов."""

    def __init__(self, stale_after_s: float = 30.0):
        self._cache: dict[str, PythTick] = {}
        self._stale_after_s = stale_after_s
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_latest(self, symbol: str) -> PythTick | None:
        """Вернуть последний tick для symbol, либо None если stale/не было.

        Stale = публикация Pyth была >stale_after_s сек назад. Возвращает None,
        чтобы детектор не действовал на устаревшие данные.
        """
        tick = self._cache.get(symbol)
        if tick is None:
            return None
        now_ms = int(time.time() * 1000)
        if now_ms - tick.received_at_ms > self._stale_after_s * 1000:
            return None
        return tick

    def all_latest(self) -> dict[str, PythTick]:
        """Все не-stale tick'и (snapshot)."""
        now_ms = int(time.time() * 1000)
        return {s: t for s, t in self._cache.items() if now_ms - t.received_at_ms <= self._stale_after_s * 1000}

    async def start(self) -> None:
        """Запустить SSE-цикл в background. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._stream_loop())

    async def stop(self) -> None:
        """Остановить SSE цикл."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._connected = False

    async def _stream_loop(self) -> None:
        """Долгоживущий SSE с exponential backoff на disconnect."""
        backoff = 1.0
        params = [("ids[]", v) for v in FEEDS.values()] + [("parsed", "true")]
        while not self._stop_event.is_set():
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)) as client:
                    async with client.stream("GET", HERMES_URL, params=params) as response:
                        response.raise_for_status()
                        self._connected = True
                        backoff = 1.0
                        logger.info("pyth_stream_connected")
                        async for line in response.aiter_lines():
                            if self._stop_event.is_set():
                                break
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if not payload:
                                continue
                            self._handle_message(payload)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, OSError) as exc:
                self._connected = False
                logger.warning(
                    "pyth_stream_disconnect",
                    error=str(exc),
                    backoff_s=round(backoff, 1),
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
        self._connected = False
        logger.info("pyth_stream_stopped")

    def _handle_message(self, raw: str) -> None:
        """Распарсить один SSE message и обновить cache."""
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        parsed = msg.get("parsed")
        if not isinstance(parsed, list):
            return
        now_ms = int(time.time() * 1000)
        for entry in parsed:
            tick = self._parse_entry(entry, now_ms)
            if tick is not None:
                self._cache[tick.symbol] = tick

    @staticmethod
    def _parse_entry(entry: dict[str, Any], now_ms: int) -> PythTick | None:
        """Конвертировать Pyth message entry → PythTick. None если невалидно."""
        feed_id_raw = entry.get("id")
        if not isinstance(feed_id_raw, str):
            return None
        feed_id = "0x" + feed_id_raw if not feed_id_raw.startswith("0x") else feed_id_raw
        symbol = _FEED_TO_SYMBOL.get(feed_id)
        if symbol is None:
            return None
        price_block = entry.get("price")
        if not isinstance(price_block, dict):
            return None
        try:
            mantissa = int(price_block["price"])
            expo = int(price_block["expo"])
            publish_time = int(price_block["publish_time"])
        except (KeyError, ValueError, TypeError):
            return None
        price = mantissa * (10.0**expo)
        confidence: float | None = None
        if "conf" in price_block:
            try:
                confidence = int(price_block["conf"]) * (10.0**expo)
            except (ValueError, TypeError):
                confidence = None
        return PythTick(
            symbol=symbol,
            price=price,
            publish_time_ms=publish_time * 1000,
            received_at_ms=now_ms,
            confidence=confidence,
        )


# Singleton — service создаётся один раз при app startup.
_singleton: PythStreamService | None = None


def get_pyth_stream() -> PythStreamService:
    """Lazy singleton. Создаёт сервис при первом обращении."""
    global _singleton
    if _singleton is None:
        _singleton = PythStreamService()
    return _singleton


# --- Helpers для discovery crypto-updown markets ---

# Slug pattern: {coin}-updown-{horizon}-{unix_ts}
#   coin ∈ {btc, eth, sol, xrp}
#   horizon ∈ {5m, 15m, 4h, ...}
#   unix_ts — unix timestamp resolution window (выровнен)
_COIN_TO_SYMBOL: dict[str, str] = {
    "btc": "BTC/USD",
    "eth": "ETH/USD",
    "sol": "SOL/USD",
    "xrp": "XRP/USD",
}


def symbol_for_slug(slug: str) -> str | None:
    """Извлечь Pyth symbol из Polymarket slug.

    Args:
        slug: e.g. "btc-updown-5m-1779354900"

    Returns:
        "BTC/USD" / "ETH/USD" / "SOL/USD" / "XRP/USD" / None.
    """
    if not slug or "-updown-" not in slug:
        return None
    coin = slug.split("-", 1)[0].lower()
    return _COIN_TO_SYMBOL.get(coin)
