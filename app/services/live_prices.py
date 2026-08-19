"""Живые цены из WS-кэша microstructure с fallback на markets.current_price.

Детекция по 15-минутному снапшоту — audit топ-3; кэш покрывает top-подписку
по объёму + рынки открытых позиций, на остальных fallback обязателен.

State-кэш семантика (fix 2026-07-03): отсутствие тика = цена НЕ изменилась,
поэтому снапшот валиден, пока WS-соединение живо. Живость доказывают два
fail-closed гейта: снапшот записан в текущей эпохе соединения (ws:conn_epoch,
инвалидирует данные прошлых коннектов за O(1) без SCAN) и heartbeat
ws:last_message_at свежее 120с (half-open защита).
"""

import time
from datetime import UTC, datetime

import structlog

from app.core.cache import cache_get, cache_set

logger = structlog.get_logger(__name__)

_HEARTBEAT_MAX_AGE_SEC = 120.0

# REST-посев (fix 2026-07-03): Polymarket НЕ шлёт массовый initial dump на
# subscribe (измерено: 169 ключей за 4ч при ~10k подписок) — спокойный рынок
# не получает live-цену, пока не тикнет. Для позиций книга сеется REST'ом.
# Валидность посева ограничена TTL ключа — НЕ штамповать его epoch'ой: не
# тикающий рынок отдавался бы как live все 6 часов state-TTL.
REST_SEED_TTL_SEC = 900
_SEED_THROTTLE_SEC = 240


async def is_ws_snapshot_live(micro: dict | None) -> bool:
    """Fail-closed гейт свежести WS-снапшота — обязателен для ВСЕХ читателей
    microstructure-кэша после перехода на state-TTL (свежесть по TTL умерла).

    Payload с epoch судится строго по epoch+heartbeat (WS-путь, приоритетный —
    merged посев+WS-partial попадает сюда). Чистый REST-посев валиден по
    построению: ключ существует ⇒ он моложе REST_SEED_TTL_SEC.
    """
    if not micro:
        return False
    if "epoch" in micro:
        epoch_rec = await cache_get("ws:conn_epoch")
        if not epoch_rec or micro.get("epoch") != epoch_rec.get("epoch"):
            return False
        heartbeat = await cache_get("ws:last_message_at")
        try:
            hb_ts = float(heartbeat.get("ts")) if heartbeat else 0.0
        except (TypeError, ValueError):
            hb_ts = 0.0
        return time.time() - hb_ts <= _HEARTBEAT_MAX_AGE_SEC
    return micro.get("source") == "rest_seed"


async def seed_book_snapshot(yes_token_id: str | None) -> bool:
    """REST-посев книги в microstructure:{token} для рынка без WS-тиков.

    Throttle SETNX 240с на токен — безопасно звать каждый минутный тик MtM.
    Первый же WS-тик перезапишет снапшот epoch-путём. Возвращает True только
    при фактической записи.
    """
    if not yes_token_id:
        return False
    try:
        from app.core.cache import get_redis

        redis = await get_redis()
        if redis is not None:
            ok = await redis.set(f"seed:throttle:{yes_token_id}", "1", nx=True, ex=_SEED_THROTTLE_SEC)
            if not ok:
                return False

        from app.services.polymarket_poller_service import get_poller_service
        from app.services.polymarket_ws_service import PolymarketWebSocketService

        book = await get_poller_service().fetch_order_book_rest(yes_token_id)
        if not book:
            return False
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids and not asks:
            return False
        micro = PolymarketWebSocketService._calculate_microstructure(bids, asks)
        micro["source"] = "rest_seed"
        micro["updated_at"] = datetime.now(UTC).isoformat()
        await cache_set(f"microstructure:{yes_token_id}", micro, ttl_seconds=REST_SEED_TTL_SEC)
        return True
    except Exception as e:
        logger.debug("seed_book_snapshot_failed", token=str(yes_token_id)[:16], error=str(e)[:80])
        return False


async def get_live_yes_price(yes_token_id: str | None, db_price: float) -> tuple[float, bool]:
    """(цена, is_live). midpoint из WS-кэша, иначе db_price."""
    if not yes_token_id:
        return db_price, False
    micro = await cache_get(f"microstructure:{yes_token_id}")
    if not micro:
        return db_price, False
    mid = micro.get("midpoint")
    try:
        mid = float(mid)
    except (TypeError, ValueError):
        return db_price, False
    if not (0.0 < mid < 1.0):
        return db_price, False
    if not await is_ws_snapshot_live(micro):
        return db_price, False
    return mid, True
