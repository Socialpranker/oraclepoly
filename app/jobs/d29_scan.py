"""D29 Scan — emit NegRisk basket arb signals on overpriced events.

⚠ DISABLED by default (audit code_audit_2026-05-14.md), флаг с 2026-07-02
(Phase 3 Task 4 — audit research-gap):
  D29 эмитит ОДИН Signal на event, но basket-arb требует execution n=3..6 NO
  buys одновременно (atomic). Раньше execution pipeline умела только
  single-leg fill (см. v2_execution.py) — partial fill ломал invariant
  «sum(NO)=n-1» и оставлял нас в exposure 1-leg.

  Теперь есть app/services/basket_execution.py (multi-leg координатор:
  depth-check всех ног, последовательные FOK, unwind при partial fill) и
  _process_signal (v2_execution.py) роутит D29-сигналы туда вместо
  single-leg пути. Но пока settings.d29_basket_enabled=False (default) —
  `run_d29_scan` возвращает empty result сразу, детектор НЕ эмитит: basket
  execution должен пройти virtual-прогон (2-4 недели) прежде чем включать
  emit на проде даже в virtual. Human decision — см.
  docs/superpowers/plans/2026-07-02-audit-fixes-phase3.md Task 4.

Every 15 min (когда enabled):
1. Find neg_risk events с >=3 markets с volume_24h >= 100 (real trading).
2. Compute sum(yes_prices) per event.
3. If sum > 1.05 (MIN) and event_id not in open_events → call detect().
4. Emit Signal с primary market_id; evidence содержит full basket detail.
5. Idempotency: 6h bucket tx_hash per event_id.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.config import get_settings
from app.detectors import d29_negrisk_basket_arb
from app.detectors.contract import Signal
from app.detectors.token_resolver import yes_token_id
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch
from app.services.detector_helpers import bucket_tx_hash, fetch_open_event_ids
from app.services.live_prices import get_live_yes_price

logger = structlog.get_logger()


_NEGRISK_MARKETS_SQL = """
SELECT m.polymarket_id AS market_id,
       m.condition_id,
       m.category,
       m.current_price AS yes_price,
       m.volume_24h,
       m.end_date,
       m.clob_token_ids,
       e.id AS event_internal_id,
       e.polymarket_id AS event_id,
       e.title AS event_title
  FROM markets m
  JOIN events e ON e.id = m.event_id
 WHERE e.neg_risk = true
   AND m.is_active = true
   AND m.resolved = false
   AND m.current_price IS NOT NULL
   AND m.current_price > 0.005
   AND m.current_price < 0.995
   AND COALESCE(m.volume_24h, 0) >= 100
   AND m.end_date IS NOT NULL
   AND m.end_date > now() + INTERVAL '24 hours'
   AND m.current_price NOT BETWEEN 0.499 AND 0.501  -- skip default placeholder
"""


async def run_d29_scan() -> dict[str, Any]:
    # Phase 3 Task 4 (audit research-gap): basket execution coordinator существует
    # (app/services/basket_execution.py), но emit остаётся выключен по умолчанию —
    # d29_basket_enabled=False пока не пройден virtual-прогон (2-4 недели) и не
    # принято явное решение владельца. См. docs/detector_lifecycle.md.
    if not get_settings().d29_basket_enabled:
        logger.info("d29_scan_disabled_basket_execution_flag_off")
        return {
            "markets": 0,
            "events": 0,
            "candidates": 0,
            "emitted": 0,
            "inserted": 0,
            "disabled": True,
        }
    rows = await fetch(_NEGRISK_MARKETS_SQL)
    if not rows:
        logger.info("d29_scan_no_negrisk_markets")
        return {"markets": 0, "events": 0, "candidates": 0, "emitted": 0, "inserted": 0}

    open_events = await fetch_open_event_ids(d29_negrisk_basket_arb.DETECTOR_NAME)

    # Live-обогащение ДО группировки в by_event: sum_yes (arb-триггер) должен
    # считаться по live-ценам, не по 15-минутному снапшоту db_price.
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    n_live_priced = 0
    for r in rows:
        event_id = r.get("event_id")
        if not event_id:
            continue
        db_price = float(r["yes_price"])
        live_price, is_live = await get_live_yes_price(yes_token_id(r.get("clob_token_ids")), db_price)
        if is_live:
            n_live_priced += 1
        by_event[event_id].append(
            {
                "market_id": r["market_id"],
                "condition_id": r["condition_id"],
                "category": r["category"],
                "yes_price": live_price,
                "volume_24h": float(r.get("volume_24h") or 0),
                "end_date": r["end_date"],
                "clob_token_ids": r["clob_token_ids"],
                "event_id": event_id,
                "event_title": r.get("event_title"),
            }
        )

    repo = get_detector_signal_repository()
    now = datetime.now(UTC)

    n_candidates = 0
    n_emitted = 0
    n_inserted = 0
    n_skipped_open = 0
    n_skipped_thin = 0

    for event_id, markets in by_event.items():
        if len(markets) < d29_negrisk_basket_arb.MIN_MARKETS:
            n_skipped_thin += 1
            continue

        sum_yes = sum(m["yes_price"] for m in markets)
        if sum_yes <= d29_negrisk_basket_arb.MIN_SUM_THRESHOLD:
            continue

        n_candidates += 1

        if event_id in open_events:
            n_skipped_open += 1
            continue

        basket_event = {
            "event_id": event_id,
            "event_title": markets[0].get("event_title"),
            "markets": markets,
        }
        sig = d29_negrisk_basket_arb.detect(basket_event)
        if sig is None:
            continue
        n_emitted += 1

        # Idempotency: 6h bucket per event_id
        tx_hash = bucket_tx_hash(
            bucket_id=event_id,
            ts=now,
            bucket_hours=6,
            namespace="d29",
        )
        evidence = dict(sig.evidence)
        evidence["tx_hash"] = tx_hash
        final_sig = Signal(
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

        try:
            if await repo.insert(final_sig):
                n_inserted += 1
                open_events.add(event_id)
        except Exception as exc:
            logger.warning(
                "d29_scan_insert_failed",
                event_id=event_id,
                error=str(exc),
            )

    logger.info(
        "d29_scan_completed",
        markets=len(rows),
        events_total=len(by_event),
        candidates=n_candidates,
        emitted=n_emitted,
        inserted=n_inserted,
        skipped_open=n_skipped_open,
        skipped_thin=n_skipped_thin,
        live_priced=f"{n_live_priced}/{len(rows)}",
    )
    return {
        "markets": len(rows),
        "events": len(by_event),
        "candidates": n_candidates,
        "emitted": n_emitted,
        "inserted": n_inserted,
        "skipped_open_event": n_skipped_open,
        "skipped_thin": n_skipped_thin,
        "live_priced": n_live_priced,
    }
