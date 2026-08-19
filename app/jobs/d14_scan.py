"""D14 Scan — раз в час эмитит CryptoWinnersDistributeNO signals.

Триггер snapshot-driven:
1. Найти active Crypto markets с current_price ∈ [0.50, 0.70), end_date в окне
   4-72h (full trigger, opened 2026-05-28 — 4h-only band [4,12)h структурно пуст
   на текущем Polymarket; 12-72h НЕ re-validated, demote по net_pnl страхует),
   vol_24h >= $5K.
2. Группировать markets by event_id (per-event dedup).
3. Skip events с открытыми D14 сигналами.
4. Для каждого market: fetch trades from Polymarket data-api → compute
   wallet quality features (uses shared _fetch_wallet_quality from d13_scan).
5. d14_crypto_winners_distribute_no.detect() — emit when filters pass.
6. INSERT в detector_signals.

Per-event dedup: outcomes коррелированы (как у D7b).
Idempotency: synthetic tx_hash на 4h bucket.

Wallet quality data computed live via Polymarket data-api (legacy retail_trades
table broken — see PR #419).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors import d14_crypto_winners_distribute_no
from app.detectors.contract import Signal
from app.detectors.token_resolver import yes_token_id
from app.jobs.d13_scan import _fetch_wallet_quality
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
       EXTRACT(EPOCH FROM (m.end_date - now())) / 3600.0 AS hours_to_resolve,
       e.polymarket_id AS event_id
  FROM markets m
  LEFT JOIN events e ON e.id = m.event_id
 WHERE m.is_active = true
   AND m.resolved = false
   AND m.current_price IS NOT NULL
   AND m.current_price >= 0.50
   AND m.current_price < 0.70
   AND m.end_date IS NOT NULL
   AND m.end_date > now() + INTERVAL '4 hours'
   AND m.end_date < now() + INTERVAL '72 hours'  -- full [4,72]h trigger (opened 2026-05-28; 4h-only band пуст, detector re-validates via demote)
   AND m.category = 'Crypto'
   AND COALESCE(m.volume_24h, 0) >= 1000  -- $5000→$1000: больше markets в выборке (user 2026-05-13)
"""


async def run_d14_scan() -> dict[str, Any]:
    rows = await fetch(_TRIGGER_MARKETS_SQL)
    if not rows:
        logger.info("d14_scan_no_markets")
        return {
            "markets": 0,
            "events": 0,
            "signals": 0,
            "inserted": 0,
            "skipped_open_event": 0,
            "skipped_no_wallet_data": 0,
        }

    open_events = await fetch_open_event_ids("D14_crypto_winners_distribute_no")

    # Batch wallet quality fetch (single SQL + parallel HTTP)
    condition_ids = list({r["condition_id"] for r in rows if r.get("condition_id")})
    wallet_quality = await _fetch_wallet_quality(condition_ids)

    repo = get_detector_signal_repository()
    now = datetime.now(UTC)

    by_event: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    n_skipped_no_wallet = 0
    n_live_priced = 0
    for r in rows:
        cid = r.get("condition_id")
        wq = wallet_quality.get(cid) if cid else None
        if wq is None:
            n_skipped_no_wallet += 1
            continue

        db_price = float(r["current_price"])
        live_price, is_live = await get_live_yes_price(yes_token_id(r.get("clob_token_ids")), db_price)
        if is_live:
            n_live_priced += 1

        event_id = r.get("event_id")
        market_event = {
            "market_id": r["market_id"],
            "category": r["category"],
            "question": r.get("question"),
            "entry_price": live_price,
            "hours_to_resolve": float(r["hours_to_resolve"]),
            "smart_money_taker_share": wq.get("smart_money_taker_share", 0.0),
            "taker_pnl_weighted_usd_24h": wq.get("taker_pnl_weighted_usd_24h", 0.0),
            "taker_roi_weighted_24h": wq.get("taker_roi_weighted_24h", 0.0),
            "buy_imbalance_asof": wq.get("buy_imbalance_asof", 0.5),
            "n_takers_24h_before": wq.get("n_takers_24h_before", 0),
            "clob_token_ids": r.get("clob_token_ids"),
            "tx_hash": bucket_tx_hash(bucket_id=r["market_id"], ts=now, namespace="d14"),
            "event_id": event_id,
        }
        by_event[event_id].append(market_event)

    n_signals = 0
    n_inserted = 0
    n_skipped_open = 0
    n_events_processed = 0

    for event_id, markets in by_event.items():
        if event_id is not None and event_id in open_events:
            n_skipped_open += 1
            continue
        n_events_processed += 1

        candidates: list[Signal] = []
        for market_event in markets:
            signal = d14_crypto_winners_distribute_no.detect(market_event)
            if signal is not None:
                candidates.append(signal)

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
            logger.warning(
                "d14_scan_insert_failed",
                error=str(exc),
                market=best.market_id,
                event_id=event_id,
            )

    logger.info(
        "d14_scan_completed",
        markets=len(rows),
        events_total=len(by_event),
        events_processed=n_events_processed,
        skipped_open_event=n_skipped_open,
        skipped_no_wallet=n_skipped_no_wallet,
        signals=n_signals,
        inserted=n_inserted,
        live_priced=f"{n_live_priced}/{len(rows)}",
    )
    return {
        "markets": len(rows),
        "events": len(by_event),
        "signals": n_signals,
        "inserted": n_inserted,
        "skipped_open_event": n_skipped_open,
        "skipped_no_wallet_data": n_skipped_no_wallet,
        "live_priced": n_live_priced,
    }
