"""
Trade Poll Job — timestamp cursor pagination.

Cron job that polls the Polymarket Data API for recent trades.
Uses Redis-backed cursor (last_trade_timestamp) for pagination.
"""

from typing import Any

import structlog

from app.core.cache import cache_get, cache_set
from app.core.task_queue import enqueue
from app.services.database import fetch
from app.services.polymarket_poller_service import get_poller_service

logger = structlog.get_logger()

LAST_TS_KEY = "trade_poll:last_ts"
PAGE_LIMIT = 500
MAX_PAGES = 10


async def run_trade_poll() -> dict[str, Any]:
    """Poll Polymarket API for recent trades using timestamp cursor."""
    poller = get_poller_service()

    # --- Step 1: Cursor из Redis ---
    last_ts = await cache_get(LAST_TS_KEY)

    # --- Step 2: Fetch pages using cursor ---
    all_trades: list[dict[str, Any]] = []
    cursor = str(last_ts) if last_ts else None

    for page_idx in range(MAX_PAGES):
        page = await poller.fetch_trades_since(after_ts=cursor, limit=PAGE_LIMIT)
        if not page:
            break
        all_trades.extend(page)

        if len(page) < PAGE_LIMIT:
            break

        timestamps = [t.get("timestamp", 0) for t in page if t.get("timestamp")]
        if timestamps:
            cursor = str(max(timestamps))
        else:
            break

    fetched_count = len(all_trades)
    logger.info(
        "trade_poll_fetched",
        fetched=fetched_count,
        pages=page_idx + 1 if all_trades else 0,
        cursor_used=last_ts is not None,
    )

    if not all_trades:
        return {"fetched": 0, "whales": 0, "deduped": 0, "sent": 0}

    # --- Step 3: Обновить cursor в Redis ---
    all_timestamps = [t.get("timestamp", 0) for t in all_trades if t.get("timestamp")]
    if all_timestamps:
        new_ts = str(max(all_timestamps))
        await cache_set(LAST_TS_KEY, new_ts, ttl_seconds=86400)

    # --- Step 4: Filter whale trades ---
    whale_trades = poller.filter_whale_trades(all_trades)
    whale_count = len(whale_trades)
    logger.info("trade_poll_filtered", whales=whale_count, total=fetched_count)

    if not whale_trades:
        return {"fetched": fetched_count, "whales": 0, "deduped": 0, "sent": 0}

    # --- Step 5: Normalize ---
    normalized = [poller.normalize_trade(trade) for trade in whale_trades]

    # --- Step 6: Resolve condition_id -> market_id ---
    unique_conditions = {event["condition_id"] for event in normalized if event.get("condition_id")}
    condition_to_market: dict[str, str | None] = {}
    for condition_id in unique_conditions:
        market_id = await poller.resolve_condition_to_market(condition_id)
        condition_to_market[condition_id] = market_id

    for event in normalized:
        cid = event.get("condition_id")
        if cid and cid in condition_to_market:
            event["market_id"] = condition_to_market[cid]

    # --- Step 7: Deduplicate ---
    tx_hashes = [event["tx_hash"] for event in normalized if event.get("tx_hash")]
    existing_hashes: set[str] = set()
    if tx_hashes:
        try:
            for i in range(0, len(tx_hashes), 200):
                batch = tx_hashes[i : i + 200]
                rows = await fetch(
                    "SELECT tx_hash FROM transactions WHERE tx_hash = ANY($1)",
                    batch,
                )
                existing_hashes.update(row["tx_hash"] for row in rows)
        except Exception as e:
            logger.warning("trade_poll_dedup_failed", error=str(e))

    new_events = [event for event in normalized if event.get("tx_hash") not in existing_hashes]
    deduped_count = len(normalized) - len(new_events)

    if not new_events:
        return {"fetched": fetched_count, "whales": whale_count, "deduped": deduped_count, "sent": 0}

    # --- Step 8: Enqueue ---
    sent_count = 0
    for event in new_events:
        try:
            ok = await enqueue("oracle:queue:transactions", event)
            if ok:
                sent_count += 1
        except Exception as e:
            logger.error("trade_poll_enqueue_failed", error=str(e), tx_hash=event.get("tx_hash"))

    return {
        "fetched": fetched_count,
        "whales": whale_count,
        "deduped": deduped_count,
        "sent": sent_count,
    }
