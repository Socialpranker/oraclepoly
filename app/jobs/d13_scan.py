"""D13 Scan — раз в час эмитит RetailConfidenceFade signals.

Триггер snapshot-driven:
1. Найти active markets с current_price ∈ [0.85, 0.95), end_date в окне 4-72h,
   category != Culture, vol_24h >= $5K.
2. Группировать markets by event_id (per-event dedup).
3. Skip events с открытыми D13 сигналами.
4. Для каждого market: fetch trades from Polymarket data-api за last 24h →
   compute smart_money_taker_share + n_takers_24h_before via JOIN
   wallet_smart_money table (17K smart wallets loaded weekly).
5. d13_retail_confidence_fade.detect() — emit только при retail-only takers.
6. INSERT в detector_signals.

Per-event dedup: outcomes коррелированы (как у D7b).
Idempotency: synthetic tx_hash на 4h bucket.

Note: trades fetched live from Polymarket data-api (https://data-api.polymarket.com/trades),
не из retail_trades table (legacy table, ingestion broken since 2026-04-30).
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors import d13_retail_confidence_fade
from app.detectors.contract import Signal
from app.detectors.token_resolver import yes_token_id
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch
from app.services.detector_helpers import bucket_tx_hash, fetch_open_event_ids, fetch_trades_batch, rank_signal
from app.services.live_prices import get_live_yes_price

logger = structlog.get_logger()

TRADES_WINDOW_SEC = 24 * 3600  # 24h pre-anchor


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
   AND m.current_price >= 0.85
   AND m.current_price < 0.95
   AND m.end_date IS NOT NULL
   AND m.end_date > now() + INTERVAL '4 hours'
   AND m.end_date < now() + INTERVAL '72 hours'
   AND m.category NOT IN ('Culture')
   AND COALESCE(m.volume_24h, 0) >= 5000
"""


# Wallet performance lookup — JOIN takers vs wallet_smart_money table
# Returns full quality data (used by D13 + D14)
_WALLET_PERF_LOOKUP_SQL = """
SELECT wallet, is_smart_money, realized_pnl_usd, roi_pct
  FROM wallet_smart_money
 WHERE LOWER(wallet) = ANY($1::text[])
"""


async def _fetch_wallet_quality(condition_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Compute wallet quality features per condition_id (shared by D13/D14).

    Strategy:
      1. Fetch trades from Polymarket /trades?market=... per condition_id (parallel)
      2. Filter в last 24h window
      3. Aggregate per (condition_id, taker): sum(size * price) = taker_volume
         Track buy/sell directionality для buy_imbalance computation
      4. Lookup wallet_smart_money table → realized_pnl, roi_pct, is_smart flag
      5. Compute aggregates: smart_share, taker_pnl_weighted, taker_roi_weighted,
         buy_imbalance_asof
    """
    if not condition_ids:
        return {}

    cutoff_ts = int(time.time()) - TRADES_WINDOW_SEC

    trades_by_cid = await fetch_trades_batch(condition_ids, cutoff_ts=cutoff_ts)

    # Per market: aggregate per wallet (volume + buy/sell split)
    # by_market[cid] = {wallet: {"volume": v, "buy_volume": bv}}
    by_market: dict[str, dict[str, dict[str, float]]] = {}
    all_wallets: set[str] = set()
    market_buy_volumes: dict[str, float] = defaultdict(float)  # market-level buy aggregate
    market_total_volumes: dict[str, float] = defaultdict(float)

    for cid, trades in trades_by_cid.items():
        wallet_data: dict[str, dict[str, float]] = defaultdict(lambda: {"volume": 0.0, "buy_volume": 0.0})
        for t in trades:
            wallet = t.get("proxyWallet")
            if not wallet:
                continue
            try:
                size = float(t.get("size") or 0)
                price = float(t.get("price") or 0)
            except (ValueError, TypeError):
                continue
            volume = size * price
            if volume <= 0:
                continue
            side = (t.get("side") or "").upper()
            wallet_l = wallet.lower()
            wallet_data[wallet_l]["volume"] += volume
            market_total_volumes[cid] += volume
            if side == "BUY":
                wallet_data[wallet_l]["buy_volume"] += volume
                market_buy_volumes[cid] += volume
        if wallet_data:
            by_market[cid] = dict(wallet_data)
            all_wallets.update(wallet_data.keys())

    if not all_wallets:
        return {}

    # SQL: lookup wallet metadata for ALL takers (smart flag + PnL + ROI)
    rows = await fetch(_WALLET_PERF_LOOKUP_SQL, list(all_wallets))
    wallet_meta: dict[str, dict[str, Any]] = {}
    for r in rows:
        wallet_meta[r["wallet"].lower()] = {
            "is_smart": bool(r.get("is_smart_money")),
            "pnl": float(r.get("realized_pnl_usd") or 0),
            "roi": float(r.get("roi_pct") or 0),
        }

    out: dict[str, dict[str, Any]] = {}
    for cid, wallets in by_market.items():
        total_volume = sum(w["volume"] for w in wallets.values())
        if total_volume <= 0:
            continue

        smart_volume = 0.0
        pnl_weighted = 0.0  # sum(volume * pnl) / total_volume
        roi_weighted = 0.0  # sum(volume * roi) / total_volume

        for w_addr, w_data in wallets.items():
            v = w_data["volume"]
            meta = wallet_meta.get(w_addr, {"is_smart": False, "pnl": 0.0, "roi": 0.0})
            if meta["is_smart"]:
                smart_volume += v
            pnl_weighted += v * meta["pnl"]
            roi_weighted += v * meta["roi"]

        market_buy = market_buy_volumes.get(cid, 0.0)
        market_total = market_total_volumes.get(cid, 0.0)

        out[cid] = {
            "n_takers_24h_before": len(wallets),
            "taker_volume_usd_total_24h": total_volume,
            "smart_money_taker_share": smart_volume / total_volume,
            "taker_pnl_weighted_usd_24h": pnl_weighted / total_volume,
            "taker_roi_weighted_24h": roi_weighted / total_volume,
            "buy_imbalance_asof": (market_buy / market_total) if market_total > 0 else 0.5,
        }
    return out


# Virtual-mode performance: 4/4 trades = 100% stop_loss rate, −94% ROI, WR 0%.
# Detector signal direction broken для этой live data slice. Disable до research
# refresh + parameter tuning. Reversible: set False.
D13_EMIT_DISABLED = True


async def run_d13_scan() -> dict[str, Any]:
    if D13_EMIT_DISABLED:
        logger.info("d13_scan_disabled_negative_virtual_pnl")
        return {
            "markets": 0,
            "events": 0,
            "candidates": 0,
            "emitted": 0,
            "inserted": 0,
            "disabled": True,
        }
    rows = await fetch(_TRIGGER_MARKETS_SQL)
    if not rows:
        logger.info("d13_scan_no_markets")
        return {
            "markets": 0,
            "events": 0,
            "signals": 0,
            "inserted": 0,
            "skipped_open_event": 0,
            "skipped_no_wallet_data": 0,
        }

    open_events = await fetch_open_event_ids("D13_retail_confidence_fade")

    # Batch-fetch wallet quality for all candidate condition_ids (single SQL)
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
            "smart_money_taker_share": wq["smart_money_taker_share"],
            "n_takers_24h_before": wq["n_takers_24h_before"],
            "clob_token_ids": r.get("clob_token_ids"),
            "tx_hash": bucket_tx_hash(bucket_id=r["market_id"], ts=now, namespace="d13"),
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
            signal = d13_retail_confidence_fade.detect(market_event)
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
                "d13_scan_insert_failed",
                error=str(exc),
                market=best.market_id,
                event_id=event_id,
            )

    logger.info(
        "d13_scan_completed",
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
