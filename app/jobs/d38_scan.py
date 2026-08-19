"""D38 ResolutionLag scan — каждые 5 минут.

Trigger SQL:
- is_active = true
- resolved = false
- end_date BETWEEN now() - 24h AND now() (свежая просрочка, не stuck)
- current_price в одной из near-fait zones [0.05-0.20] или [0.80-0.95]
- volume_24h >= 500

Per-event dedup (как у других detectors) — оставляем 1 best signal per event.
Per-market idempotency через 24h bucket_tx_hash (один сигнал/маркет/24ч).

Cron: every 5 minutes (high frequency — окно short-lived, до 6ч на сигнал).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors import d38_resolution_lag
from app.detectors.contract import Signal
from app.detectors.token_resolver import yes_token_id
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch
from app.services.detector_helpers import bucket_tx_hash, fetch_open_event_ids, rank_signal
from app.services.live_prices import get_live_yes_price

logger = structlog.get_logger()


_TRIGGER_MARKETS_SQL = """
SELECT m.polymarket_id AS market_id,
       m.condition_id,
       m.category,
       m.question,
       m.current_price,
       m.volume_24h,
       m.end_date,
       m.clob_token_ids,
       EXTRACT(EPOCH FROM (now() - m.end_date)) / 3600.0 AS lag_hours,
       e.polymarket_id AS event_id,
       e.title         AS event_title
  FROM markets m
  LEFT JOIN events e ON e.id = m.event_id
 WHERE m.is_active = true
   AND m.resolved  = false
   AND m.end_date IS NOT NULL
   AND m.end_date < now()
   AND m.end_date > now() - INTERVAL '24 hours'
   AND m.current_price IS NOT NULL
   AND ((m.current_price >= 0.80 AND m.current_price < 0.95)
        OR (m.current_price >= 0.05 AND m.current_price < 0.20))
   AND COALESCE(m.volume_24h, 0) >= 500
"""


async def run_d38_scan() -> dict[str, Any]:
    rows = await fetch(_TRIGGER_MARKETS_SQL)
    if not rows:
        logger.info("d38_scan_no_markets")
        return {"markets": 0, "events": 0, "signals": 0, "inserted": 0, "skipped_open_event": 0}

    open_events = await fetch_open_event_ids(d38_resolution_lag.DETECTOR_NAME)
    repo = get_detector_signal_repository()
    now = datetime.now(UTC)

    by_event: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    n_live_priced = 0
    for r in rows:
        # Fix-4 (latency-аудит №3): live-override как в d3_scan. markets.current_price —
        # 15-минутный снапшот market_sync, а скан бежит каждые 5 минут; зона и
        # entry_price зависят от этой цены. WS-кэш свежее; нет/протух → db-цена.
        db_price = float(r["current_price"])
        live_price, is_live = await get_live_yes_price(yes_token_id(r.get("clob_token_ids")), db_price)
        if is_live:
            n_live_priced += 1
        by_event[r.get("event_id")].append(
            {
                "market_id": r["market_id"],
                "condition_id": r["condition_id"],
                "category": r["category"],
                "question": r.get("question"),
                "event_title": r.get("event_title"),
                "current_price": live_price,
                "volume_24h": float(r.get("volume_24h") or 0),
                "end_date": r["end_date"],
                "lag_hours": float(r["lag_hours"]),
                "clob_token_ids": r.get("clob_token_ids"),
                "event_id": r.get("event_id"),
            }
        )

    n_signals = n_inserted = n_skipped_open = 0

    for event_id, markets in by_event.items():
        if event_id is not None and event_id in open_events:
            n_skipped_open += 1
            continue

        candidates: list[Signal] = []
        for me in markets:
            sig = d38_resolution_lag.detect(me)
            if sig is None:
                continue
            # 24h bucket per-market dedup (нет смысла эмитить тот же маркет несколько раз)
            tx_hash = bucket_tx_hash(bucket_id=me["market_id"], ts=now, bucket_hours=24, namespace="d38")
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
        n_signals += 1
        try:
            if await repo.insert(best):
                n_inserted += 1
                if event_id is not None:
                    open_events.add(event_id)
        except Exception as exc:
            logger.warning("d38_scan_insert_failed", error=str(exc), market=best.market_id)

    logger.info(
        "d38_scan_completed",
        markets=len(rows),
        events=len(by_event),
        signals=n_signals,
        inserted=n_inserted,
        skipped_open=n_skipped_open,
        live_priced=n_live_priced,
    )
    return {
        "markets": len(rows),
        "events": len(by_event),
        "signals": n_signals,
        "inserted": n_inserted,
        "skipped_open_event": n_skipped_open,
        "live_priced": n_live_priced,
    }
