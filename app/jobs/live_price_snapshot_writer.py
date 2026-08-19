"""Live price snapshot writer — каждые 5 min ловит yes_price на anchor moment.

Для каждого active market m с end_date > now + 1h:
  Для каждого anchor h ∈ {4, 24, 72, 168, 720}:
    Если anchor_ts = m.end_date - h "проходит" через текущий момент (в окне ±5 мин),
    то писать row (condition_id, hours_to_resolve=h, yes_price=current_price).

Источник цены — Redis cache `microstructure:{token_id}` (WS push, ~1ms read).
Fallback на REST если cache miss — копирует pattern из virtual_execution.py:63-88.

Idempotency: PRIMARY KEY (condition_id, hours_to_resolve) → ON CONFLICT DO UPDATE
сохраняет последнюю цену если cron сработает дважды на тот же anchor moment.

См. docs/superpowers/plans/bright-hopping-meteor.md (Day 2).
"""

from __future__ import annotations

from typing import Any

import structlog

from app.core.cache import cache_get
from app.detectors.token_resolver import resolve_token_id
from app.ml.feature_kernel import CANONICAL_ANCHORS
from app.services.database import execute, fetch

logger = structlog.get_logger(__name__)

# Окно вокруг anchor moment в которое ловим цену.
# Cron работает каждые 5 min, поэтому окно ±300s (≈5 min total) гарантирует поимку
# каждого anchor_ts хотя бы один раз. Если cron не успел — fallback ловится в feature
# builder (он LEFT JOIN-ит snapshot, NULL переходит в COALESCE/нули).
ANCHOR_WINDOW_SEC: int = 300

# Min 24h volume для inclusion. Markets с <$5K vol_24h — мусор для ML, не трогаем.
# Используем volume_24h т.к. lifetime_volume_usd нет в Postgres markets table
# (это поле только в canonical markets_clean.parquet).
MIN_VOLUME_24H_USD: float = 5000.0


_ACTIVE_MARKETS_SQL = """
SELECT m.condition_id,
       m.end_date,
       m.clob_token_ids
  FROM markets m
 WHERE m.is_active = true
   AND m.resolved = false
   AND m.condition_id IS NOT NULL
   AND m.end_date IS NOT NULL
   AND m.end_date > now() + INTERVAL '1 hour'
   AND m.clob_token_ids IS NOT NULL
   AND COALESCE(m.volume_24h, 0) >= $1
"""

_UPSERT_SNAPSHOT_SQL = """
INSERT INTO live_price_snapshots (condition_id, hours_to_resolve, yes_price, captured_at)
VALUES ($1, $2, $3, now())
ON CONFLICT (condition_id, hours_to_resolve)
DO UPDATE SET yes_price = EXCLUDED.yes_price, captured_at = EXCLUDED.captured_at
"""


async def _get_yes_price(token_id_yes: str) -> float | None:
    """Read mid price из microstructure cache. Fallback REST если miss.

    Returns mid = (best_bid + best_ask) / 2 чтобы избежать spread bias.
    """
    if not token_id_yes:
        return None
    try:
        ws = await cache_get(f"microstructure:{token_id_yes}")
    except Exception:
        ws = None
    from app.services.live_prices import is_ws_snapshot_live

    # State-TTL (2026-07-03): протухший снапшот записал бы фейковую цену в датасет
    if ws and isinstance(ws, dict) and await is_ws_snapshot_live(ws):
        b = ws.get("best_bid")
        a = ws.get("best_ask")
        if b is not None and a is not None:
            try:
                bf, af = float(b), float(a)
                if 0.0 < bf < 1.0 and 0.0 < af < 1.0:
                    return round((bf + af) / 2.0, 6)
            except (TypeError, ValueError):
                pass

    # REST fallback (rarely needed — most active markets are in WS cache)
    #
    # **Polymarket book ordering** (critical bug found 2026-05-14):
    #   bids ascending price → best bid = max(bid_prices)
    #   asks descending price → best ask = min(ask_prices)
    # Старый код брал [0] index → получал worst-of-book (placeholder 0.01/0.99
    # walls). Mid = (0.01 + 0.99) / 2 = 0.5 → 76% snapshots на 4h anchor
    # имели fake price=0.5. ML predictor видел fake mid-zone и emit'ил huge
    # fake edge claims.
    try:
        from app.services.polymarket_poller_service import get_poller_service

        book = await get_poller_service().fetch_order_book_rest(token_id_yes)
    except Exception:
        return None
    if not book:
        return None
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    if not asks or not bids:
        return None
    try:
        bid_prices = [float(b["price"]) for b in bids]
        ask_prices = [float(a["price"]) for a in asks]
        bid = max(bid_prices)
        ask = min(ask_prices)
        if not (0.0 < bid < 1.0 and 0.0 < ask < 1.0):
            return None
        if bid >= ask:
            return None
        return round((bid + ask) / 2.0, 6)
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def _anchor_passing_now(end_date_unix: float, anchor_h: int, now_unix: float) -> bool:
    """Возвращает True если anchor_ts = end_date - h находится в окне ±ANCHOR_WINDOW_SEC от now."""
    anchor_unix = end_date_unix - anchor_h * 3600.0
    return abs(anchor_unix - now_unix) <= ANCHOR_WINDOW_SEC


async def run_live_price_snapshot_writer() -> dict[str, Any]:
    """Один тик: для каждого active market записать price для anchors прошедших now."""
    import time

    rows = await fetch(_ACTIVE_MARKETS_SQL, MIN_VOLUME_24H_USD)
    if not rows:
        logger.info("live_price_snapshot_no_active_markets")
        return {"markets": 0, "snapshots_written": 0, "no_price": 0}

    now_unix = time.time()

    n_written = 0
    n_no_price = 0
    n_no_anchor = 0

    for r in rows:
        end_date = r["end_date"]
        if end_date is None:
            continue
        end_unix = end_date.timestamp()

        # Какие anchors сейчас "проходят"?
        passing = [h for h in CANONICAL_ANCHORS if _anchor_passing_now(end_unix, h, now_unix)]
        if not passing:
            n_no_anchor += 1
            continue

        # Извлекаем YES token id через universal resolver (handles JSONB string from asyncpg)
        token_id_yes = resolve_token_id(r.get("clob_token_ids"), "YES")
        if not token_id_yes:
            continue

        price = await _get_yes_price(token_id_yes)
        if price is None:
            n_no_price += 1
            continue

        for h in passing:
            try:
                await execute(_UPSERT_SNAPSHOT_SQL, r["condition_id"], h, price)
                n_written += 1
            except Exception as exc:
                logger.warning(
                    "live_price_snapshot_upsert_failed",
                    condition_id=r["condition_id"],
                    anchor_h=h,
                    error=str(exc)[:200],
                )

    logger.info(
        "live_price_snapshot_done",
        markets=len(rows),
        snapshots_written=n_written,
        no_price=n_no_price,
        no_anchor_passing=n_no_anchor,
    )
    return {
        "markets": len(rows),
        "snapshots_written": n_written,
        "no_price": n_no_price,
        "no_anchor_passing": n_no_anchor,
    }
