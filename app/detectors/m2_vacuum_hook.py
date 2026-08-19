"""M2 liquidity vacuum — WSS realtime hook.

Детекция вакуума на hot-path WSS (_handle_market_message). Лёгкий O(1)
depletion-check без await; тяжёлое (Redis throttle, DB-fetch, detect, insert,
record_event) — в фоновом _m2_emit_async (fire-and-forget asyncio.create_task).

Ошибки в _m2_emit_async НЕ пропагируются (try/except + logger.warning) —
WSS-поток не должен падать от M2.

Спек §3: docs/superpowers/specs/2026-06-21-m2-liquidity-vacuum-design.md
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.cache import cache_get, get_redis
from app.core.config import get_settings
from app.detectors import m2_liquidity_vacuum
from app.services.database import fetchrow
from app.services.detector_helpers import bucket_tx_hash

logger = structlog.get_logger()

THROTTLE_SEC: int = 600  # один M2-сигнал на рынок раз в 10 мин
WHALE_FLAG_TTL: int = 90  # буфер ≥ 60s окна
MAX_DEPLETION_RATIO: float = m2_liquidity_vacuum.MAX_DEPLETION_RATIO


def _m2_enabled() -> bool:
    """Feature-flag (default off). Читается на hot-path — дёшево (lru_cache settings)."""
    return bool(get_settings().m2_vacuum_enabled)


def _m2_depletion_check(micro: dict[str, Any]) -> str | None:
    """Вернуть 'bid'|'ask' если одна сторона опустела, иначе None. O(1), чистая.

    Лёгкий ratio-чек на hot-path (полные гейты mid-зона/floor — в detect()).
    """
    db = micro.get("depth_bid", 0.0)
    da = micro.get("depth_ask", 0.0)
    hi, lo = (db, da) if db >= da else (da, db)
    if hi <= 0:
        return None
    if (lo / hi) >= MAX_DEPLETION_RATIO:
        return None
    return "ask" if da < db else "bid"


async def _whale_in_window(market_id: str) -> bool:
    """Был ли whale-trade (≥$20K) на этом рынке за ~60s. Флаг ставит whale_watcher."""
    return bool(await cache_get(f"m2:whale_seen:{market_id}"))


async def _m2_emit_async(
    market_id: str,
    token_id: str,
    microstructure: dict[str, Any],
    depleted_side: str,
) -> None:
    """Фоновый emit: throttle → fetch → whale-flag → detect → insert → record_event.

    Fire-and-forget. Все ошибки логируются, не пропагируются (WSS не падает).
    """
    try:
        # 1. Realtime-throttle: один вакуум на рынок в окне THROTTLE_SEC.
        #    get_redis() (cache.py:43) → raw aioredis-клиент для атомарного SETNX.
        redis = await get_redis()
        if redis is not None:
            ok = await redis.set(f"m2:throttle:{market_id}", "1", nx=True, ex=THROTTLE_SEC)
            if not ok:
                return  # вакуум уже зафиксирован

        # 2. Market-контекст (как whale_watcher:210).
        #    polymarket_id обязателен: detector_signals.market_id конвенция =
        #    markets.polymarket_id (0x), НЕ внутренний UUID markets.id — иначе
        #    resolve-JOIN (detector_signals_resolve) не матчится и сигнал censored.
        #    event_id = events.polymarket_id (та же конвенция, эталон d38_scan.py):
        #    UUID markets.event_id ломал event-кластеризацию аналитики. NULL допустим.
        row = await fetchrow(
            "SELECT m.polymarket_id, m.condition_id, m.clob_token_ids, m.category, m.current_price, "
            "e.polymarket_id AS event_id "
            "FROM markets m LEFT JOIN events e ON e.id = m.event_id WHERE m.id = $1",
            market_id,
        )
        if not row:
            return
        polymarket_id = row.get("polymarket_id")
        if not polymarket_id:
            logger.warning("m2_emit_no_polymarket_id", market_id=str(market_id)[:20])
            return

        # 3. Whale-флаг (confound-контроль с D1/D26).
        whale = await _whale_in_window(market_id)

        generated_at = datetime.now(UTC)
        tx_hash = bucket_tx_hash(bucket_id=str(market_id), ts=generated_at, bucket_hours=1, namespace="m2")

        # 4. detect() (чистая) на собранном market_event.
        #    clob_token_ids → detect() резолвит YES-token (yes_token_id);
        #    token_id (WSS asset) — fallback если clob_token_ids пуст.
        market_event = {
            "market_id": str(polymarket_id),
            "event_id": row.get("event_id"),
            "token_id": token_id,
            "clob_token_ids": row.get("clob_token_ids"),
            "category": row.get("category"),
            "yes_midpoint": microstructure.get("midpoint"),
            "depth_bid": microstructure.get("depth_bid", 0.0),
            "depth_ask": microstructure.get("depth_ask", 0.0),
            "depleted_side": depleted_side,
            "depletion_ratio": _ratio(microstructure),
            "whale_in_window": whale,
            "detected_at": generated_at.isoformat(),
            "tx_hash": tx_hash,
        }
        signal = m2_liquidity_vacuum.detect(market_event)
        if signal is None:
            return

        # 5. INSERT (ON CONFLICT DO NOTHING по uq_detector_signals_m2_tx).
        from app.repositories.detector_signal_repository import DetectorSignalRepository

        await DetectorSignalRepository().insert(signal)

        # 6. record_event: orderbook ±60s для пост-hoc EDA (fire&forget внутри).
        cond = row.get("condition_id")
        clob = row.get("clob_token_ids") or []
        if isinstance(clob, str):
            import json as _json

            try:
                clob = _json.loads(clob)
            except (ValueError, TypeError):
                clob = []
        token_ids = [str(t) for t in clob if t]
        if cond and token_ids:
            from app.services.snapshot_recorder import record_event

            await record_event(
                trigger_type="liquidity_vacuum",
                market_id=str(market_id),
                condition_id=str(cond),
                token_ids=token_ids,
                trigger_meta={
                    "depleted_side": depleted_side,
                    "depletion_ratio": market_event["depletion_ratio"],
                    "whale_in_window": whale,
                },
            )
    except Exception as exc:  # noqa: BLE001 — M2 не должен ронять WSS
        logger.warning("m2_emit_failed", market_id=str(market_id)[:20], error=str(exc))


def _ratio(micro: dict[str, Any]) -> float:
    db = float(micro.get("depth_bid", 0.0))
    da = float(micro.get("depth_ask", 0.0))
    hi, lo = max(db, da), min(db, da)
    return (lo / hi) if hi > 0 else 1.0
