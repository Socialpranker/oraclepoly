"""D45 Chainlink lead-lag scan — crypto-updown markets (Shadow mode).

Что делает:
    1. Выбирает active updown markets (BTC/ETH/SOL/XRP), чьё end_date в окне
       резолюции [now + 5s, now + 30s].
    2. Для каждого: парсит slug → symbol/window_start_ts, читает live Pyth
       price (PythStreamService cache) + price_at_window_open (Pyth REST history
       через Redis cache).
    3. Вызывает чистый detector `d45_chainlink_leadlag.detect()`.
    4. Эмитит Signal в detector_signals при divergence > threshold.

Cadence:
    Cron каждые 5 секунд (быстрее, чем у других детекторов — окно резолюции
    короткое). Реализуем через crontab + 60-секундный loop с `sleep(5)` (см.
    `scripts/cron_runner.py`, команда `d45-chainlink-leadlag-scan`).

Idempotency:
    Synthetic tx_hash = sha256(market_id|d45_chainlink_leadlag|window_end_ts).
    Один Signal на market на резолюционное окно — повторные scan'ы попадают
    в ON CONFLICT DO NOTHING.

Failure modes (все non-fatal, logger.warning):
    - PythStreamService disconnected → all skipped (нужен restart lifespan).
    - Pyth REST history недоступен → skip market этот тик, повторим через 5s.
    - Empty markets fetch → log empty, exit 0.
"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors import d45_chainlink_leadlag as d45
from app.detectors.contract import Signal
from app.detectors.token_resolver import resolve_token_id
from app.repositories.detector_signal_repository import (
    DetectorSignalRepository,
    get_detector_signal_repository,
)
from app.services.binance_stream_service import BinanceStreamService, get_binance_stream
from app.services.database import fetch
from app.services.pyth_history_service import get_price_at
from app.services.pyth_stream_service import PythStreamService, get_pyth_stream, symbol_for_slug

logger = structlog.get_logger(__name__)

DETECTOR_NAME = d45.DETECTOR_NAME

SCAN_WINDOW_MIN_SEC = d45.DEFAULT_MIN_SECONDS_TO_RESOLUTION
SCAN_WINDOW_MAX_SEC = d45.DEFAULT_MAX_SECONDS_TO_RESOLUTION

# Pyth tick должен быть свежим — fallback safeguard если stream stuck
PYTH_FRESH_TTL_S = 30.0

# Только короткие horizons резолвятся Polymarket'ом через Chainlink Data Streams.
# Monthly/weekly markets резолвятся через Binance (см. chainlink_alternative.md §5.5).
# Если slug имеет другой horizon — Pyth НЕ proxy для truth, signal будет ложным.
CHAINLINK_HORIZON_WHITELIST_SEC: frozenset[int] = frozenset({300, 900, 14400})  # 5m, 15m, 4h

_ACTIVE_UPDOWN_SQL = """
-- market_id = markets.polymarket_id (0x-condition_id) — конвенция detector_signals
-- (эталон d38_scan.py). Внутренний UUID (markets.id) ломает resolve-JOIN → censored.
-- event_id = events.polymarket_id (та же конвенция): UUID markets.event_id ломал
-- event-кластеризацию аналитики (category_performance, money-funnel). NULL допустим.
SELECT m.polymarket_id   AS market_id,
       m.condition_id    AS condition_id,
       m.slug            AS slug,
       e.polymarket_id   AS event_id,
       m.clob_token_ids  AS clob_token_ids,
       m.current_price   AS current_price,
       EXTRACT(EPOCH FROM m.end_date)::bigint AS end_date_ts
  FROM public.markets m
  LEFT JOIN events e ON e.id = m.event_id
 WHERE m.is_active = true
   AND m.resolved  = false
   AND m.slug LIKE '%-updown-%'
   AND m.end_date IS NOT NULL
   AND m.current_price IS NOT NULL
   AND m.end_date BETWEEN now() + ($1 || ' seconds')::interval
                      AND now() + ($2 || ' seconds')::interval
"""


def _coerce_yes_midpoint(raw: Any) -> float | None:
    """Конвертировать markets.current_price → float YES midpoint.

    current_price = numeric(10,6) в БД, asyncpg возвращает Decimal. По
    Polymarket convention для bin markets это уже YES price (0..1).
    Decimal → float должен пройти clean; невалидные значения → None.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not (0.0 < value < 1.0):
        # 0.0 или 1.0 — рынок практически уже зарешён, detector такое
        # отсечёт сам, но фильтруем заранее для observability.
        return None
    return value


def _market_window_tx_hash(market_id: str, window_end_ts: int) -> str:
    """Idempotency hash: один сигнал per (market, window_end). Window_end_ts
    стабилен в пределах окна резолюции — все 5-30s scan'ы попадают в один
    bucket. Префикс 0x + 62 hex sym = совместим с UNIQUE constraint."""
    raw = f"{market_id}|{DETECTOR_NAME}|{window_end_ts}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:62]


def _build_market_state(row: dict[str, Any]) -> d45.MarketState | None:
    """Конвертировать БД-строку в MarketState. None если row невалиден."""
    slug = row.get("slug")
    if not isinstance(slug, str):
        return None
    parsed = d45.parse_slug(slug)
    if parsed is None:
        return None
    _coin, horizon_sec, window_start_ts = parsed
    # Защита от monthly/weekly markets (резолвятся Binance, не Chainlink).
    # Без этого guard'а мы бы эмитили signal на Pyth basis vs реальный Binance fix.
    if horizon_sec not in CHAINLINK_HORIZON_WHITELIST_SEC:
        return None
    window_end_ts = window_start_ts + horizon_sec

    yes_mid = _coerce_yes_midpoint(row.get("current_price"))
    if yes_mid is None:
        return None

    market_id = row.get("market_id")
    condition_id = row.get("condition_id")
    if not market_id or not condition_id:
        return None

    # Пробрасываем raw clob_token_ids — to_signal резолвит токен по detection.side
    # (NO-сигнал → NO-токен). token_id=YES оставлен как legacy fallback.
    clob_token_ids = row.get("clob_token_ids")
    token_id = resolve_token_id(clob_token_ids, "YES")
    event_id = row.get("event_id")

    return d45.MarketState(
        market_id=str(market_id),
        condition_id=str(condition_id),
        slug=slug,
        yes_midpoint=yes_mid,
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        horizon_sec=horizon_sec,
        event_id=str(event_id) if event_id else None,
        token_id=token_id,
        clob_token_ids=clob_token_ids,
    )


async def _build_pyth_snapshot(
    market: d45.MarketState,
    stream: PythStreamService,
    now_ms: int,
) -> tuple[d45.PythSnapshot | None, str | None]:
    """Вернуть (snapshot, skip_reason).

    skip_reason используется в structured log для observability:
    'no_symbol', 'no_live_tick', 'stale_tick', 'no_open_price'.
    """
    symbol = symbol_for_slug(market.slug)
    if symbol is None:
        return None, "no_symbol"

    tick = stream.get_latest(symbol)
    if tick is None:
        return None, "no_live_tick"

    age_s = (now_ms - tick.received_at_ms) / 1000.0
    if age_s > PYTH_FRESH_TTL_S:
        return None, "stale_tick"

    open_price = await get_price_at(symbol, market.window_start_ts)
    if open_price is None or open_price <= 0:
        return None, "no_open_price"

    snap = d45.PythSnapshot(
        symbol=symbol,
        price_now=tick.price,
        price_at_window_open=open_price,
        publish_time_ms=tick.publish_time_ms,
    )
    return snap, None


async def _emit_signal(
    detection: d45.D45Detection,
    market: d45.MarketState,
    pyth: d45.PythSnapshot,
    now_dt: datetime,
    now_ms: int,
    repo: DetectorSignalRepository,
) -> bool:
    """Build Signal, прикрепить tx_hash, INSERT. True если вставлено."""
    signal = d45.to_signal(
        detection,
        market,
        pyth,
        generated_at=now_dt,
        pyth_publish_lag_ms=max(0, now_ms - pyth.publish_time_ms),
    )

    # to_signal уже резолвит token_id по detection.side (NO→NO-токен).
    tx_hash = _market_window_tx_hash(market.market_id, market.window_end_ts)
    evidence = dict(signal.evidence)
    evidence["tx_hash"] = tx_hash
    evidence["bucket"] = "market_window"

    signal_with_tx = Signal(
        detector=signal.detector,
        market_id=signal.market_id,
        side=signal.side,
        generated_at=signal.generated_at,
        confidence=signal.confidence,
        expected_edge_pct=signal.expected_edge_pct,
        fair_price=signal.fair_price,
        current_price=signal.current_price,
        horizon_sec=signal.horizon_sec,
        token_id=signal.token_id,
        event_id=signal.event_id,
        evidence=evidence,
    )

    try:
        return await repo.insert(signal_with_tx)
    except Exception as exc:
        logger.warning(
            "d45_chainlink_leadlag_insert_failed",
            error=str(exc),
            market_id=signal_with_tx.market_id,
        )
        return False


async def run_d45_chainlink_leadlag_scan(
    *,
    stream: PythStreamService | None = None,
    binance: BinanceStreamService | None = None,
    repo: DetectorSignalRepository | None = None,
    now_ts: int | None = None,
) -> dict[str, int]:
    """Один проход scanner'а.

    Args:
        stream: PythStreamService instance. Если None — берём global singleton.
            В тестах подменяем фейком.
        binance: BinanceStreamService для cross-source sanity check. Если None
            (или не connected) — sanity check выключается, detect работает по
            одному Pyth (legacy behaviour).
        repo: DetectorSignalRepository. None → singleton.
        now_ts: явный unix timestamp (sec) для детерминизма тестов. None → time.time().

    Returns:
        {'scanned': N, 'signals': K, 'inserted': M,
         'skipped_no_symbol': ..., 'skipped_no_live_tick': ...,
         'skipped_stale_tick': ..., 'skipped_no_open_price': ...,
         'skipped_no_detection': ...}
    """
    stream = stream or get_pyth_stream()
    binance = binance if binance is not None else get_binance_stream()
    repo = repo or get_detector_signal_repository()

    now_ts_val = int(now_ts if now_ts is not None else time.time())
    now_dt = datetime.fromtimestamp(now_ts_val, tz=UTC)
    now_ms = now_ts_val * 1000

    rows = await fetch(_ACTIVE_UPDOWN_SQL, str(SCAN_WINDOW_MIN_SEC), str(SCAN_WINDOW_MAX_SEC))
    if not rows:
        logger.debug("d45_chainlink_leadlag_no_markets")
        return {
            "scanned": 0,
            "signals": 0,
            "inserted": 0,
            "skipped_no_symbol": 0,
            "skipped_no_live_tick": 0,
            "skipped_stale_tick": 0,
            "skipped_no_open_price": 0,
            "skipped_no_detection": 0,
        }

    stats: dict[str, int] = {
        "scanned": len(rows),
        "signals": 0,
        "inserted": 0,
        "skipped_no_symbol": 0,
        "skipped_no_live_tick": 0,
        "skipped_stale_tick": 0,
        "skipped_no_open_price": 0,
        "skipped_no_detection": 0,
    }

    for raw_row in rows:
        row = dict(raw_row)
        market = _build_market_state(row)
        if market is None:
            stats["skipped_no_symbol"] += 1
            continue

        snap, skip_reason = await _build_pyth_snapshot(market, stream, now_ms)
        if snap is None:
            stats[f"skipped_{skip_reason}"] += 1
            continue

        # Optional cross-check от Binance. Если tick нет / stale — передаём
        # None и detect просто skip'ает sanity check (не блокирующий).
        binance_price_now: float | None = None
        if binance is not None:
            btick = binance.get_latest(snap.symbol)
            if btick is not None:
                binance_price_now = btick.price

        detection = d45.detect(
            market,
            snap,
            now_ts=now_ts_val,
            binance_price_now=binance_price_now,
        )
        if detection is None:
            stats["skipped_no_detection"] += 1
            continue

        stats["signals"] += 1
        if await _emit_signal(detection, market, snap, now_dt, now_ms, repo):
            stats["inserted"] += 1

    logger.info("d45_chainlink_leadlag_completed", **stats)
    return stats
