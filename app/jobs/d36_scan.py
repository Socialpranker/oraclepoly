"""Microstructure scan — каждые 30 минут.

Scan для D36 OBI Pressure (orderbook imbalance) поверх CLOB book.
Один проход → fetch book → run detector → emit signals.

Pipeline:
  Detector → Signal table → v2_execution → execution_orders → position_manager

(D37_depth_decay удалён 2026-06-20 — убыточен на live: mean realized_roi_net −6.7%
на чистом WIN/LOSS, см. detector_triage. Раньше пиггибэкал этот scan, emit был
отключён с 2026-05-28.)
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors import d36_obi_pressure
from app.detectors.contract import Signal
from app.detectors.d36_obi_pressure import compute_obi_metrics
from app.detectors.token_resolver import yes_token_id
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch
from app.services.detector_helpers import bucket_tx_hash, fetch_open_event_ids, rank_signal
from app.services.live_prices import get_live_yes_price

logger = structlog.get_logger()


_CONCURRENCY = 5
_MAX_MARKETS_PER_TICK = 200


_TRIGGER_MARKETS_SQL = """
SELECT m.polymarket_id AS market_id,
       m.condition_id,
       m.category,
       m.question,
       m.current_price,
       m.volume_24h,
       m.end_date,
       m.clob_token_ids,
       EXTRACT(EPOCH FROM (m.end_date - now())) / 3600.0 AS hours_to_resolve,
       e.polymarket_id AS event_id
  FROM markets m
  LEFT JOIN events e ON e.id = m.event_id
 WHERE m.is_active = true
   AND m.resolved  = false
   AND m.current_price IS NOT NULL
   AND m.current_price >= 0.30
   AND m.current_price <= 0.85
   AND m.end_date IS NOT NULL
   AND m.end_date > now() + INTERVAL '4 hours'
   AND m.end_date < now() + INTERVAL '72 hours'
   AND COALESCE(m.volume_24h, 0) >= 1000
   AND m.clob_token_ids IS NOT NULL
 ORDER BY COALESCE(m.volume_24h, 0) DESC
 LIMIT 200
"""


async def _fetch_book_for_market(
    market: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> tuple[dict[str, Any], dict[str, float] | None]:
    """Fetch CLOB book для market, compute OBI metrics."""
    yes_token = yes_token_id(market.get("clob_token_ids"))
    if not yes_token:
        return market, None

    from app.services.polymarket_poller_service import get_poller_service

    async with semaphore:
        try:
            book = await get_poller_service().fetch_order_book_rest(yes_token)
        except Exception as exc:
            logger.debug("d36_book_fetch_error", market=market["market_id"][:16], error=str(exc)[:100])
            return market, None

    if not book:
        return market, None

    metrics = compute_obi_metrics(book)
    return market, metrics


async def run_d36_scan() -> dict[str, Any]:
    rows = await fetch(_TRIGGER_MARKETS_SQL)
    if not rows:
        logger.info("d36_scan_no_markets")
        return {"markets": 0, "events": 0, "signals": 0, "inserted": 0}

    # Parallel book fetch
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    fetch_tasks = [_fetch_book_for_market(dict(r), semaphore) for r in rows]
    results = await asyncio.gather(*fetch_tasks, return_exceptions=False)

    open_events_d36 = await fetch_open_event_ids(d36_obi_pressure.DETECTOR_NAME)
    repo = get_detector_signal_repository()
    now = datetime.now(UTC)

    by_event: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    n_with_book = 0
    n_live_priced = 0
    for market, metrics in results:
        if metrics is None:
            continue
        n_with_book += 1
        db_price = float(market["current_price"])
        live_price, is_live = await get_live_yes_price(yes_token_id(market.get("clob_token_ids")), db_price)
        if is_live:
            n_live_priced += 1
        market_event = {
            "market_id": market["market_id"],
            "category": market["category"],
            "question": market.get("question"),
            "current_price": live_price,
            "hours_to_resolve": float(market["hours_to_resolve"]),
            "clob_token_ids": market.get("clob_token_ids"),
            "event_id": market.get("event_id"),
            "book_metrics": metrics,
        }
        by_event[market.get("event_id")].append(market_event)

    n_d36 = n_inserted = 0
    for event_id, markets in by_event.items():
        if event_id is not None and event_id in open_events_d36:
            continue
        candidates: list[Signal] = []
        for me in markets:
            sig = d36_obi_pressure.detect(me)
            if sig is None:
                continue
            tx_hash = bucket_tx_hash(bucket_id=me["market_id"], ts=now, bucket_hours=2, namespace="d36")
            evidence = dict(sig.evidence)
            evidence["tx_hash"] = tx_hash
            sig = Signal(
                detector=sig.detector,
                market_id=sig.market_id,
                side=sig.side,
                generated_at=sig.generated_at,
                confidence=sig.confidence,
                expected_edge_pct=sig.expected_edge_pct,
                fair_price=sig.fair_price,
                current_price=sig.current_price,
                horizon_sec=sig.horizon_sec,
                token_id=sig.token_id,
                event_id=sig.event_id,
                evidence=evidence,
            )
            candidates.append(sig)
        if not candidates:
            continue
        best = max(candidates, key=rank_signal)
        n_d36 += 1
        try:
            if await repo.insert(best):
                n_inserted += 1
                if event_id is not None:
                    open_events_d36.add(event_id)
        except Exception as exc:
            logger.warning(
                "microstructure_scan_insert_failed",
                detector="d36",
                error=str(exc),
                market=best.market_id,
            )

    summary = {
        "markets": len(rows),
        "books_fetched": n_with_book,
        "events": len(by_event),
        "signals_d36": n_d36,
        "signals": n_d36,
        "inserted": n_inserted,
        "live_priced": n_live_priced,
    }
    logger.info("microstructure_scan_completed", **summary)
    return summary
