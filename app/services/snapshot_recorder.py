"""Event-triggered orderbook snapshot recorder.

Аудит 2026-05-21 C.6 gap #5 (приоритет 4): event-shock snapshots вместо
continuous depth persistence (грабли depth_total scalar, см. clob_book_sync.py).

Architecture:
- `OrderbookRingbuffer`: per-token deque c maxlen=120 (60s past × 2/s update),
  заполняется WSS market channel при наличии активного recorder.
- `record_event()`: при триггере (whale trade ≥$50k, spread spike, UMA dispute):
  - Берёт прошлые 60s из ringbuffer (если есть)
  - Spawn'ит background task который добирает forward window +60s через
    snapshot updates в Redis (TTL=60s уже работает).
  - Batch INSERT all snapshots в orderbook_event_snapshots.

Memory bounded:
- Ringbuffer активен только для top-500 markets (refresh периодически).
- Forward window не использует buffer — читает Redis.

Phase 1 (этот PR): только T1 whale_trade trigger + forward-only window (т.к.
ringbuffer pre-population требует hook'а в WSS, который в этом PR не
включаем — будет следующий итерация). Текущая реализация записывает
0..+60s через Redis polling.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core import cache as _cache_module
from app.core.cache import cache_get
from app.services.database import executemany

logger = structlog.get_logger()


# Окно ±60s от события. Phase 1 — только forward (0..+60s).
WINDOW_BACKWARD_SECONDS = 60
WINDOW_FORWARD_SECONDS = 60
FORWARD_POLL_INTERVAL_SECONDS = 1.0  # читаем Redis раз в секунду

# Hard cap: не больше 240 snapshots per event (sanity).
MAX_SNAPSHOTS_PER_EVENT = 240

# Redis-backed ringbuffer (cross-process: WSS пишет в backend, recorder читает в worker).
RING_MAXLEN = 120  # 60s × ~2 update/s
RING_TTL_SECONDS = 180  # буфер живёт дольше окна сбора


_INSERT_SNAPSHOT_SQL = """
INSERT INTO orderbook_event_snapshots (
    event_id, trigger_type, trigger_meta,
    market_id, condition_id, token_id,
    captured_at, t_offset_ms,
    best_bid, best_ask, midpoint, spread,
    bids_ladder, asks_ladder,
    depth_bid_usd, depth_ask_usd
)
VALUES (
    $1, $2, $3::jsonb,
    $4, $5, $6,
    $7, $8,
    $9, $10, $11, $12,
    $13::jsonb, $14::jsonb,
    $15, $16
)
ON CONFLICT (event_id, token_id, captured_at) DO NOTHING
"""


def _get_redis():
    """Raw redis client (shared между процессами). None если не инициализирован."""
    return _cache_module._redis


async def ring_push(token_id: str, snapshot: dict[str, Any], *, ts_unix: float) -> None:
    """Положить microstructure snapshot в Redis-ringbuffer по token_id.

    Вызывается из WSS на каждый book-update. lpush + ltrim + expire —
    тот же паттерн, что microstructure_history в polymarket_ws_service.
    Cross-process: WSS пишет в backend-контейнере, record_event читает в worker.
    """
    redis = _get_redis()
    if redis is None:
        return
    item = dict(snapshot)
    item["ts_unix"] = ts_unix
    key = f"obs_ring:{token_id}"
    try:
        await redis.lpush(key, json.dumps(item))
        await redis.ltrim(key, 0, RING_MAXLEN - 1)
        await redis.expire(key, RING_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — ringbuffer не должен ронять WSS
        # warning, не debug: тихий отказ ring_push = пустое backward-окно whale-shock
        # (prod LOG_LEVEL=info скрыл бы debug). Именно эту потерю мы час диагностировали.
        logger.warning("ring_push_failed", token_id=token_id[:20], error=str(exc))


async def ring_get_recent(token_id: str, *, since_ts: float) -> list[dict[str, Any]]:
    """Прочитать backward-окно: snapshots с ts_unix >= since_ts."""
    redis = _get_redis()
    if redis is None:
        return []
    key = f"obs_ring:{token_id}"
    try:
        raw_items = await redis.lrange(key, 0, RING_MAXLEN - 1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ring_get_recent_failed", token_id=token_id[:20], error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for raw in raw_items:
        try:
            snap = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if snap.get("ts_unix", 0) >= since_ts:
            out.append(snap)
    return out


def _microstructure_to_snapshot_row(
    *,
    event_id: uuid.UUID,
    trigger_type: str,
    trigger_meta: dict[str, Any] | None,
    market_id: str,
    condition_id: str,
    token_id: str,
    captured_at: datetime,
    t_offset_ms: int,
    microstructure: dict[str, Any],
) -> tuple:
    """Преобразовать Redis microstructure dict → DB row."""
    levels = microstructure.get("levels") or {}
    return (
        str(event_id),
        trigger_type,
        json.dumps(trigger_meta) if trigger_meta is not None else None,
        market_id,
        condition_id,
        token_id,
        captured_at,
        t_offset_ms,
        microstructure.get("best_bid"),
        microstructure.get("best_ask"),
        microstructure.get("midpoint"),
        microstructure.get("spread"),
        json.dumps(levels.get("bids", [])),
        json.dumps(levels.get("asks", [])),
        microstructure.get("depth_bid"),
        microstructure.get("depth_ask"),
    )


def get_poller_service():
    """Lazy accessor — вынесен на уровень модуля для патчинга в тестах."""
    from app.services.polymarket_poller_service import get_poller_service as _get

    return _get()


async def _rest_fallback_row(
    *,
    event_id: uuid.UUID,
    trigger_type: str,
    trigger_meta: dict[str, Any] | None,
    market_id: str,
    condition_id: str,
    token_id: str,
    event_t0: datetime,
) -> tuple | None:
    """REST-фоллбэк: добрать 1 снимок через CLOB /book когда ring пуст.

    Whale-shock на средне-ликвидном рынке часто не имеет ring-снимков
    (obs_ring TTL × отсутствие свежих WSS book-updates). Без фоллбэка
    backward-окно пустое → 0 снимков на событие. CLOB /book работает с
    decimal token_id (markets.clob_token_ids уже decimal).

    Снимок проставляется на event_t0 (t_offset_ms=0): REST-запрос идёт в
    момент обработки триггера, т.е. ≈ момент события.

    Returns:
        Готовая DB-row (tuple) или None если book пуст / REST недоступен.
    """
    book = await get_poller_service().fetch_order_book_rest(token_id)
    if not book:
        return None
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids and not asks:
        return None

    from app.services.polymarket_ws_service import PolymarketWebSocketService

    micro = PolymarketWebSocketService._calculate_microstructure(bids, asks)
    return _microstructure_to_snapshot_row(
        event_id=event_id,
        trigger_type=trigger_type,
        trigger_meta=trigger_meta,
        market_id=market_id,
        condition_id=condition_id,
        token_id=token_id,
        captured_at=event_t0,
        t_offset_ms=0,
        microstructure=micro,
    )


async def _collect_forward_window(
    *,
    event_id: uuid.UUID,
    trigger_type: str,
    trigger_meta: dict[str, Any] | None,
    market_id: str,
    condition_id: str,
    token_ids: list[str],
    event_t0: datetime,
    duration_seconds: int = WINDOW_FORWARD_SECONDS,
    prefill_rows: list[tuple] | None = None,
) -> int:
    """Async task: каждую секунду читать Redis cache и копить snapshots.

    Args:
        prefill_rows: backward-окно из ringbuffer (готовые DB-rows), собранное
            синхронно в record_event. Мержится с forward-снимками в один INSERT.

    Returns:
        Количество rows записанных в БД.
    """
    rows: list[tuple] = list(prefill_rows) if prefill_rows else []
    seen_keys: set[tuple[str, str]] = set()  # (token_id, captured_at_iso) — dedup
    # backward-снимки уже в rows → регистрируем в seen, чтобы forward не задублировал.
    for r in rows:
        token_id_r, captured_at_r = r[5], r[6]
        seen_keys.add(
            (token_id_r, captured_at_r.isoformat() if hasattr(captured_at_r, "isoformat") else str(captured_at_r))
        )

    end_time = event_t0.timestamp() + duration_seconds
    while datetime.now(UTC).timestamp() < end_time and len(rows) < MAX_SNAPSHOTS_PER_EVENT:
        for token_id in token_ids:
            ms = await cache_get(f"microstructure:{token_id}")
            if not ms:
                continue
            updated_at_iso = ms.get("updated_at")
            if not updated_at_iso:
                continue
            key = (token_id, updated_at_iso)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            try:
                captured_at = datetime.fromisoformat(updated_at_iso)
            except (ValueError, TypeError):
                continue
            t_offset_ms = int((captured_at - event_t0).total_seconds() * 1000)
            rows.append(
                _microstructure_to_snapshot_row(
                    event_id=event_id,
                    trigger_type=trigger_type,
                    trigger_meta=trigger_meta,
                    market_id=market_id,
                    condition_id=condition_id,
                    token_id=token_id,
                    captured_at=captured_at,
                    t_offset_ms=t_offset_ms,
                    microstructure=ms,
                )
            )
        await asyncio.sleep(FORWARD_POLL_INTERVAL_SECONDS)

    if not rows:
        logger.info(
            "snapshot_recorder_no_data",
            event_id=str(event_id),
            trigger_type=trigger_type,
            market_id=market_id,
        )
        return 0

    try:
        await executemany(_INSERT_SNAPSHOT_SQL, rows)
    except Exception as exc:
        logger.warning(
            "snapshot_recorder_db_insert_failed",
            event_id=str(event_id),
            n_rows=len(rows),
            error=str(exc),
        )
        return 0

    logger.info(
        "snapshot_recorder_window_complete",
        event_id=str(event_id),
        trigger_type=trigger_type,
        market_id=market_id,
        rows_inserted=len(rows),
        token_ids_count=len(token_ids),
    )
    return len(rows)


async def record_event(
    *,
    trigger_type: str,
    market_id: str,
    condition_id: str,
    token_ids: list[str],
    trigger_meta: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Записать orderbook snapshots вокруг события.

    Backward-окно [-60s, 0] собирается синхронно из Redis-ringbuffer (данные
    уже там). Forward-окно [0, +60s] — фоновый таск (non-blocking). Оба
    мержатся в один batch-INSERT внутри forward-таска.

    Args:
        trigger_type: 'whale_trade', 'spread_spike', 'uma_dispute', и т.д.
        market_id: UUID markets.id (строка).
        condition_id: Polymarket condition id.
        token_ids: CLOB token ids для market'а (обычно 2: YES + NO).
        trigger_meta: контекст триггера ({"trade_size": 50000, ...}).

    Returns:
        event_id (UUID) — для логирования и cross-reference.
    """
    if not token_ids:
        logger.warning("snapshot_recorder_no_tokens", market_id=market_id)
        return uuid.uuid4()

    event_id = uuid.uuid4()
    event_t0 = datetime.now(UTC)
    since_ts = event_t0.timestamp() - WINDOW_BACKWARD_SECONDS

    # Backward-окно из ringbuffer (синхронно — данные уже в Redis).
    prefill_rows: list[tuple] = []
    for token_id in token_ids:
        ring_snaps = await ring_get_recent(token_id, since_ts=since_ts)
        for snap in ring_snaps:
            ts_unix = snap.get("ts_unix")
            if ts_unix is None:
                continue
            captured_at = datetime.fromtimestamp(ts_unix, tz=UTC)
            t_offset_ms = int((captured_at - event_t0).total_seconds() * 1000)
            prefill_rows.append(
                _microstructure_to_snapshot_row(
                    event_id=event_id,
                    trigger_type=trigger_type,
                    trigger_meta=trigger_meta,
                    market_id=market_id,
                    condition_id=condition_id,
                    token_id=token_id,
                    captured_at=captured_at,
                    t_offset_ms=t_offset_ms,
                    microstructure=snap,
                )
            )
        # Ring пуст для token_id → REST-фоллбэк (1 снимок на event_t0).
        if not ring_snaps:
            fallback_row = await _rest_fallback_row(
                event_id=event_id,
                trigger_type=trigger_type,
                trigger_meta=trigger_meta,
                market_id=market_id,
                condition_id=condition_id,
                token_id=token_id,
                event_t0=event_t0,
            )
            if fallback_row is not None:
                prefill_rows.append(fallback_row)

    asyncio.create_task(
        _collect_forward_window(
            event_id=event_id,
            trigger_type=trigger_type,
            trigger_meta=trigger_meta,
            market_id=market_id,
            condition_id=condition_id,
            token_ids=token_ids,
            event_t0=event_t0,
            prefill_rows=prefill_rows,
        )
    )

    logger.info(
        "snapshot_recorder_event_scheduled",
        event_id=str(event_id),
        trigger_type=trigger_type,
        market_id=market_id,
        token_ids_count=len(token_ids),
        backward_rows=len(prefill_rows),
    )
    return event_id
