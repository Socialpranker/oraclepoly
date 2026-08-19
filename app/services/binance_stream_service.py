"""Binance public WS — independent cross-check для Pyth.

Назначение:
    Pyth — single source. Если у него glitch (paused publisher, stale slot,
    malformed price), мы рискуем эмитить ложный D45 signal. Binance даёт
    independent price stream без auth (`stream.binance.com:9443/ws`).

    Не primary — Chainlink CEXprice = aggregate Binance+Coinbase+Kraken+
    Bitstamp, basis 5-30$. Binance используется только для **sanity check**:
    если |pyth_price - binance_price| > MAX_BASIS → пропускаем signal,
    Pyth скорее всего сломан.

Cadence:
    Binance trade stream даёт 100-500+ trades/sec на топовых парах. Берём
    last trade price как latest snapshot.

Reconnect:
    WS auto-close через 24h. Exponential backoff 1→60s, как у Pyth.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import structlog
import websockets

logger = structlog.get_logger(__name__)

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"

# Symbol → Binance stream name (lowercase, trades stream).
# Map ровно тех же 4 символов как Pyth для cross-check.
SYMBOLS_TO_STREAM: dict[str, str] = {
    "BTC/USD": "btcusdt@trade",
    "ETH/USD": "ethusdt@trade",
    "SOL/USD": "solusdt@trade",
    "XRP/USD": "xrpusdt@trade",
}

# Обратный mapping: stream name (`btcusdt@trade`) → наш symbol (`BTC/USD`).
_STREAM_TO_SYMBOL: dict[str, str] = {v: k for k, v in SYMBOLS_TO_STREAM.items()}


@dataclass(frozen=True)
class BinanceTick:
    """Last trade tick от Binance."""

    symbol: str
    price: float
    trade_time_ms: int  # E timestamp от Binance
    received_at_ms: int


class BinanceStreamService:
    """Async WS consumer от Binance combined stream. In-memory cache."""

    def __init__(self, stale_after_s: float = 30.0):
        self._cache: dict[str, BinanceTick] = {}
        self._stale_after_s = stale_after_s
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_latest(self, symbol: str) -> BinanceTick | None:
        """Вернуть последний tick или None если stale/не было."""
        tick = self._cache.get(symbol)
        if tick is None:
            return None
        now_ms = int(time.time() * 1000)
        if now_ms - tick.received_at_ms > self._stale_after_s * 1000:
            return None
        return tick

    async def start(self) -> None:
        """Запустить WS loop в background. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._stream_loop())

    async def stop(self) -> None:
        """Остановить WS loop."""
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
        """Долгоживущий WS с exponential backoff."""
        backoff = 1.0
        # Combined stream subscribe via path: /stream?streams=btcusdt@trade/ethusdt@trade
        streams = "/".join(SYMBOLS_TO_STREAM.values())
        url = f"{BINANCE_WS_URL}/{streams}"

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, open_timeout=15, ping_interval=20) as ws:
                    self._connected = True
                    backoff = 1.0
                    logger.info("binance_stream_connected", n_symbols=len(SYMBOLS_TO_STREAM))
                    while not self._stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=60.0)
                        except TimeoutError:
                            # 60s no data — abnormal на trade stream, reconnect
                            logger.warning("binance_stream_idle_timeout")
                            break
                        self._handle_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                logger.warning(
                    "binance_stream_disconnect",
                    error=str(exc),
                    backoff_s=round(backoff, 1),
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)

        self._connected = False
        logger.info("binance_stream_stopped")

    def _handle_message(self, raw: str | bytes) -> None:
        """Распарсить trade message.

        Binance trade event schema:
            {"e":"trade","E":1779...,"s":"BTCUSDT","p":"77123.45",...}
        Combined stream wraps в {"stream":"btcusdt@trade","data":{...}}.
        """
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return

        # Combined stream wrapper
        if "stream" in msg and "data" in msg:
            stream_name = msg.get("stream")
            data = msg.get("data") or {}
        else:
            stream_name = None
            data = msg

        symbol = None
        if isinstance(stream_name, str):
            symbol = _STREAM_TO_SYMBOL.get(stream_name)
        if symbol is None:
            # fallback: derive from s field (e.g. "BTCUSDT")
            s_field = data.get("s") if isinstance(data, dict) else None
            if isinstance(s_field, str):
                stream_guess = f"{s_field.lower()}@trade"
                symbol = _STREAM_TO_SYMBOL.get(stream_guess)
        if symbol is None:
            return

        tick = self._parse_trade(data, symbol)
        if tick is not None:
            self._cache[symbol] = tick

    @staticmethod
    def _parse_trade(data: dict[str, Any], symbol: str) -> BinanceTick | None:
        """Parse Binance trade event → BinanceTick."""
        if not isinstance(data, dict):
            return None
        try:
            price = float(data["p"])
            trade_time = int(data["E"])
        except (KeyError, ValueError, TypeError):
            return None
        return BinanceTick(
            symbol=symbol,
            price=price,
            trade_time_ms=trade_time,
            received_at_ms=int(time.time() * 1000),
        )


# Singleton
_singleton: BinanceStreamService | None = None


def get_binance_stream() -> BinanceStreamService:
    """Lazy singleton."""
    global _singleton
    if _singleton is None:
        _singleton = BinanceStreamService()
    return _singleton
