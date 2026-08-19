"""D26 Scan — каждые 5 мин эмитит Fade-Whale Extreme signals.

Триггер trade-driven:
1. Найти active markets с current_price < 0.15, vol_24h >= $5K, end_date > now + 4h.
2. Per-event dedup (skip events с open D26 signals).
3. Fetch trades from Polymarket /trades?market=cid за last 15 min per market.
4. Filter: takers ∈ FADE_WHALE cohort (Postgres wallet_alchemy_classes).
5. Aggregate: sum(usd_amount), price_drift в окне.
6. If push >= $3K AND drift > 0 → emit D26 signal (buy NO).

Idempotency: synthetic tx_hash на 15-min bucket per market.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors import d26_fade_whale_extreme
from app.detectors.contract import Signal
from app.detectors.token_resolver import yes_token_id
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch
from app.services.detector_helpers import (
    bucket_tx_hash,
    fetch_open_event_ids,
    fetch_trades_batch,
    rank_signal,
)
from app.services.live_prices import get_live_yes_price

logger = structlog.get_logger()

PUSH_WINDOW_SEC = 15 * 60  # 15 min lookback для wallet pushes


_TRIGGER_MARKETS_SQL = """
SELECT m.polymarket_id   AS market_id,
       m.condition_id,
       m.category,
       m.question,
       m.current_price,
       m.volume_24h,
       m.end_date,
       m.clob_token_ids,
       EXTRACT(EPOCH FROM (m.end_date - now())) / 3600.0 AS hours_to_resolve,
       e.polymarket_id   AS event_id
  FROM markets m
  LEFT JOIN events e ON e.id = m.event_id
 WHERE m.is_active = true
   AND m.resolved  = false
   AND m.current_price IS NOT NULL
   AND m.current_price < 0.15
   AND m.end_date IS NOT NULL
   AND m.end_date > now() + INTERVAL '4 hours'
   AND COALESCE(m.volume_24h, 0) >= 5000
"""

_FADE_WHALE_SET_SQL = """
SELECT wallet FROM wallet_alchemy_classes WHERE class_label = 'fade_whale'
"""


async def _load_fade_whale_set() -> set[str]:
    rows = await fetch(_FADE_WHALE_SET_SQL)
    return {r["wallet"].lower() for r in rows}


async def _aggregate_fade_whale_pushes(
    condition_ids: list[str],
    fade_whale_set: set[str],
) -> dict[str, dict[str, float]]:
    """Per condition_id, агрегируем FADE_WHALE taker pushes в last 15 min.

    Returns: {cid: {"push_usd": v, "drift_pp": d}}.
    """
    if not condition_ids or not fade_whale_set:
        return {}

    cutoff_ts = int(time.time()) - PUSH_WINDOW_SEC
    trades_by_cid = await fetch_trades_batch(condition_ids, cutoff_ts=cutoff_ts)

    out: dict[str, dict[str, float]] = {}
    for cid, trades in trades_by_cid.items():
        whale_trades = []
        for t in trades:
            wallet = (t.get("proxyWallet") or "").lower()
            if wallet not in fade_whale_set:
                continue
            try:
                size = float(t.get("size") or 0)
                price = float(t.get("price") or 0)
                ts = int(t.get("timestamp") or 0)
            except (ValueError, TypeError):
                continue
            usd = size * price
            if usd <= 0 or price <= 0 or price >= 1:
                continue
            whale_trades.append((ts, price, usd))

        if not whale_trades:
            continue

        whale_trades.sort(key=lambda x: x[0])
        total_usd = sum(t[2] for t in whale_trades)
        # Drift = последняя цена whale - первая, по таймстемпу trades
        drift_pp = whale_trades[-1][1] - whale_trades[0][1]

        out[cid] = {
            "push_usd": total_usd,
            "drift_pp": drift_pp,
        }
    return out


async def run_d26_scan() -> dict[str, Any]:
    rows = await fetch(_TRIGGER_MARKETS_SQL)
    if not rows:
        logger.info("d26_scan_no_markets")
        return {"markets": 0, "events": 0, "signals": 0, "inserted": 0, "skipped_open_event": 0}

    fade_whale_set = await _load_fade_whale_set()
    if not fade_whale_set:
        logger.warning("d26_scan_no_fade_whale_set", reason="wallet_alchemy_classes empty")
        return {"markets": len(rows), "signals": 0, "inserted": 0, "no_cohort": True}

    open_events = await fetch_open_event_ids("D26_fade_whale_extreme")

    condition_ids = list({r["condition_id"] for r in rows if r.get("condition_id")})
    push_aggregates = await _aggregate_fade_whale_pushes(condition_ids, fade_whale_set)

    repo = get_detector_signal_repository()
    now = datetime.now(UTC)

    by_event: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    n_skipped_no_push = 0
    n_live_priced = 0
    for r in rows:
        cid = r.get("condition_id")
        if not cid or cid not in push_aggregates:
            n_skipped_no_push += 1
            continue
        push = push_aggregates[cid]

        db_price = float(r["current_price"])
        live_price, is_live = await get_live_yes_price(yes_token_id(r.get("clob_token_ids")), db_price)
        if is_live:
            n_live_priced += 1

        market_event = {
            "market_id": r["market_id"],
            "condition_id": cid,
            "category": r["category"],
            "question": r.get("question"),
            "current_price": live_price,
            "volume_24h": float(r.get("volume_24h") or 0),
            "hours_to_resolve": float(r["hours_to_resolve"]),
            "clob_token_ids": r.get("clob_token_ids"),
            "event_id": r.get("event_id"),
            "fade_whale_push_usd_15min": push["push_usd"],
            "fade_whale_drift_pp_15min": push["drift_pp"],
        }
        by_event[r.get("event_id")].append(market_event)

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
        for me in markets:
            sig = d26_fade_whale_extreme.detect(me)
            if sig is not None:
                # Add bucket tx_hash для idempotency
                me["tx_hash"] = bucket_tx_hash(bucket_id=me["market_id"], ts=now, namespace="d26")
                # Replace Signal with one that carries tx_hash through evidence
                evidence = dict(sig.evidence)
                evidence["tx_hash"] = me["tx_hash"]
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
            logger.warning("d26_scan_insert_failed", error=str(exc), market=best.market_id)

    logger.info(
        "d26_scan_completed",
        markets=len(rows),
        events_total=len(by_event),
        events_processed=n_events_processed,
        skipped_open_event=n_skipped_open,
        skipped_no_push=n_skipped_no_push,
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
        "skipped_no_push": n_skipped_no_push,
        "live_priced": n_live_priced,
    }
