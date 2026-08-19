"""D44 LateConvergenceCarry scan — каждые 30 минут.

Trigger SQL:
- is_active = true, resolved = false
- end_date BETWEEN now()+2h AND now()+7d (in late-convergence DTE window)
- current_price в одной из bands: [0.96, 0.98] (YES) или [0.02, 0.04] (NO)
- volume_24h >= $10K
- category в whitelist (sports/crypto/economics) — SQL early-filter

Liquidity берётся с events.liquidity (markets.liquidity не существует в
schema). Если event_id NULL — liquidity = 0 → detector отрежет.

Anti-double-fire с D38: D38 emits на [now-24h, now] window. D44 на
[now+2h, now+7d]. Не пересекаются.

Per-event dedup: 1 best signal per event.
Per-market idempotency: 12h bucket_tx_hash.

Cron: every 30 min (carry trade window — часы-дни, не минуты).

Phase 1 = SHADOW ONLY. Detector emits evidence.shadow_only=True. NO execution.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors import d44_late_convergence_carry
from app.detectors.contract import Signal
from app.detectors.token_resolver import yes_token_id
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch
from app.services.detector_helpers import bucket_tx_hash, fetch_open_event_ids, rank_signal
from app.services.live_prices import get_live_yes_price

logger = structlog.get_logger()


_TRIGGER_MARKETS_SQL = """
SELECT m.polymarket_id      AS market_id,
       m.condition_id,
       m.category,
       m.question,
       m.current_price,
       m.volume_24h,
       m.end_date,
       m.clob_token_ids,
       e.polymarket_id      AS event_id,
       e.title              AS event_title,
       COALESCE(e.liquidity, 0)::float8 AS liquidity,
       EXTRACT(EPOCH FROM (m.end_date - now())) / 3600.0 AS dte_hours
  FROM public.markets m
  LEFT JOIN public.events e ON e.id = m.event_id
 WHERE m.is_active = true
   AND m.resolved  = false
   AND m.end_date IS NOT NULL
   AND m.end_date > now() + INTERVAL '2 hours'
   AND m.end_date < now() + INTERVAL '168 hours'
   AND m.current_price IS NOT NULL
   AND ((m.current_price >= 0.96 AND m.current_price <= 0.98)
        OR (m.current_price >= 0.02 AND m.current_price <= 0.04))
   AND COALESCE(m.volume_24h, 0) >= 10000
   AND lower(COALESCE(m.category, '')) IN ('sports', 'crypto', 'economics')
"""


async def run_late_convergence_scan() -> dict[str, Any]:
    rows = await fetch(_TRIGGER_MARKETS_SQL)
    if not rows:
        logger.info("d44_scan_no_markets")
        return {"markets": 0, "events": 0, "signals": 0, "inserted": 0, "skipped_open_event": 0}

    open_events = await fetch_open_event_ids(d44_late_convergence_carry.DETECTOR_NAME)
    repo = get_detector_signal_repository()
    now = datetime.now(UTC)

    by_event: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    n_live_priced = 0
    for r in rows:
        db_price = float(r["current_price"])
        live_price, is_live = await get_live_yes_price(yes_token_id(r.get("clob_token_ids")), db_price)
        if is_live:
            n_live_priced += 1
        by_event[r.get("event_id")].append(
            {
                "market_id": r["market_id"],
                "condition_id": r["condition_id"],
                "category": r.get("category"),
                "question": r.get("question"),
                "current_price": live_price,
                "end_date": r["end_date"],
                "volume_24h": float(r.get("volume_24h") or 0),
                "liquidity": float(r.get("liquidity") or 0),
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
            sig = d44_late_convergence_carry.detect(me)
            if sig is None:
                continue
            tx_hash = bucket_tx_hash(bucket_id=me["market_id"], ts=now, bucket_hours=12, namespace="d44")
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
            logger.warning("d44_scan_insert_failed", error=str(exc), market=best.market_id)

    logger.info(
        "d44_scan_completed",
        markets=len(rows),
        events=len(by_event),
        signals=n_signals,
        inserted=n_inserted,
        skipped_open=n_skipped_open,
        live_priced=f"{n_live_priced}/{len(rows)}",
    )
    return {
        "markets": len(rows),
        "events": len(by_event),
        "signals": n_signals,
        "inserted": n_inserted,
        "skipped_open_event": n_skipped_open,
        "live_priced": n_live_priced,
    }
