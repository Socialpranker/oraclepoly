"""D45 Chainlink lead-lag — health/stats endpoint.

Чтобы проверить здоровье D45 без чтения docker logs:
    GET /api/d45/stats

Возвращает:
    - feature_flag_enabled — из Settings.d45_chainlink_leadlag_enabled.
    - pyth: connected + per-symbol freshness (age_s).
    - binance: connected + per-symbol freshness.
    - basis: текущий |pyth-binance| в % по каждому symbol (если оба свежие).
    - signals_recent: count по последним 1h / 24h из detector_signals.

Endpoint admin-only через `check_admin`.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends

from app.core.auth import check_admin
from app.core.config import get_settings
from app.services.binance_stream_service import (
    SYMBOLS_TO_STREAM as BINANCE_SYMBOLS,
)
from app.services.binance_stream_service import (
    get_binance_stream,
)
from app.services.database import fetchval
from app.services.pyth_stream_service import FEEDS as PYTH_FEEDS
from app.services.pyth_stream_service import get_pyth_stream

router = APIRouter()


def _symbol_status(tick, now_ms: int) -> dict[str, Any]:
    """Сериализация тика в JSON-friendly snapshot."""
    if tick is None:
        return {"fresh": False, "price": None, "age_s": None}
    age_s = (now_ms - tick.received_at_ms) / 1000.0
    return {
        "fresh": True,
        "price": tick.price,
        "age_s": round(age_s, 2),
    }


def _basis_pct(pyth_price: float | None, binance_price: float | None) -> float | None:
    """|pyth - binance| / binance × 100. None если один из источников отсутствует."""
    if pyth_price is None or binance_price is None or binance_price == 0:
        return None
    return round(abs(pyth_price - binance_price) / binance_price * 100.0, 4)


@router.get("/stats")
async def d45_stats(_admin: dict = Depends(check_admin)) -> dict[str, Any]:
    """Live health snapshot D45 stack (Pyth + Binance + signals counters).

    Дешёвый endpoint: только in-memory cache + 2 COUNT-запроса к БД.
    Без secrets / без sensitive market data.
    """
    settings = get_settings()
    pyth = get_pyth_stream()
    binance = get_binance_stream()
    now_ms = int(time.time() * 1000)

    pyth_per_symbol = {}
    binance_per_symbol = {}
    basis_per_symbol = {}

    for symbol in PYTH_FEEDS:
        p_tick = pyth.get_latest(symbol)
        b_tick = binance.get_latest(symbol) if symbol in BINANCE_SYMBOLS else None
        pyth_per_symbol[symbol] = _symbol_status(p_tick, now_ms)
        binance_per_symbol[symbol] = _symbol_status(b_tick, now_ms)
        basis_per_symbol[symbol] = _basis_pct(
            p_tick.price if p_tick else None,
            b_tick.price if b_tick else None,
        )

    signals_1h = await fetchval(
        """
        SELECT COUNT(*) FROM detector_signals
         WHERE detector = 'd45_chainlink_leadlag'
           AND generated_at > now() - interval '1 hour'
        """
    )
    signals_24h = await fetchval(
        """
        SELECT COUNT(*) FROM detector_signals
         WHERE detector = 'd45_chainlink_leadlag'
           AND generated_at > now() - interval '24 hours'
        """
    )

    return {
        "enabled": bool(settings.d45_chainlink_leadlag_enabled),
        "scan_interval_sec": float(settings.d45_chainlink_leadlag_scan_interval_sec),
        "pyth": {
            "connected": pyth.is_connected,
            "symbols": pyth_per_symbol,
        },
        "binance": {
            "connected": binance.is_connected,
            "symbols": binance_per_symbol,
        },
        "basis_pct": basis_per_symbol,
        "signals": {
            "last_1h": int(signals_1h or 0),
            "last_24h": int(signals_24h or 0),
        },
    }
