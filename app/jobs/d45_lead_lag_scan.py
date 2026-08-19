"""D45 LeadLagCrossVenue scan — каждые 5 минут.

Trigger: для каждой active market_pair_matches (Polymarket↔Kalshi) запрашиваем
последний snapshot Polymarket (oracle_snapshots) + snapshot ~10 min ago, плюс
последний и старый Kalshi orderbook snapshot (kalshi_orderbook_snapshots). Если
один venue двинулся на ≥3pp за окно, второй <1pp — emit shadow signal.

Anti-conflict с D40:
  D40 = snapshot static spread arb (instant edge).
  D45 = dynamic движение в time-series.
  Не пересекаются — разные сигналы. D40 ищет mispricing, D45 — lead-lag dynamics.

Per-pair idempotency: 30-min bucket per (poly_market_id, kalshi_ticker).

Cron: every 5 min (lag window short — PDF: 30 sec - 30 min hold).

Phase 1 = SHADOW ONLY. Без Kalshi credentials эта job будет emit 0 signals
(kalshi_orderbook_snapshots пуст). Готов к моменту когда pipeline дотащит данные.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors import d45_lead_lag_cross_venue
from app.detectors.contract import Signal
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch
from app.services.detector_helpers import bucket_tx_hash

logger = structlog.get_logger()


_PAIRED_SNAPSHOTS_SQL = """
WITH active_pairs AS (
    SELECT mp.polymarket_market_id,
           mp.kalshi_ticker,
           mp.inverse_side,
           m.id              AS poly_market_uuid,
           m.polymarket_id   AS poly_market_id,
           m.category        AS poly_category,
           m.volume_24h      AS poly_volume_24h,
           m.clob_token_ids  AS poly_clob_token_ids,
           e.polymarket_id   AS event_id
      FROM public.market_pair_matches mp
      JOIN public.markets m ON m.polymarket_id = mp.polymarket_market_id
      LEFT JOIN public.events e ON e.id = m.event_id
     WHERE mp.expired_at IS NULL
       AND m.is_active = true
       AND m.resolved  = false
       AND COALESCE(m.volume_24h, 0) >= 1000
),
poly_now AS (
    SELECT DISTINCT ON (os.market_id)
           os.market_id,
           os.market_price AS yes_price,
           os.created_at   AS captured_at
      FROM public.oracle_snapshots os
      JOIN active_pairs ap ON ap.poly_market_uuid = os.market_id
     ORDER BY os.market_id, os.created_at DESC
),
poly_old AS (
    SELECT DISTINCT ON (os.market_id)
           os.market_id,
           os.market_price AS yes_price,
           os.created_at   AS captured_at
      FROM public.oracle_snapshots os
      JOIN active_pairs ap ON ap.poly_market_uuid = os.market_id
     WHERE os.created_at <= now() - INTERVAL '10 minutes'
     ORDER BY os.market_id, os.created_at DESC
),
kalshi_now AS (
    SELECT DISTINCT ON (ks.ticker)
           ks.ticker,
           ks.yes_ask_cents,
           ks.captured_at,
           km.volume_24h_contracts,
           km.last_price_cents
      FROM public.kalshi_orderbook_snapshots ks
      JOIN active_pairs ap ON ap.kalshi_ticker = ks.ticker
      LEFT JOIN public.kalshi_markets km ON km.ticker = ks.ticker
     ORDER BY ks.ticker, ks.captured_at DESC
),
kalshi_old AS (
    SELECT DISTINCT ON (ks.ticker)
           ks.ticker,
           ks.yes_ask_cents,
           ks.captured_at
      FROM public.kalshi_orderbook_snapshots ks
      JOIN active_pairs ap ON ap.kalshi_ticker = ks.ticker
     WHERE ks.captured_at <= now() - INTERVAL '10 minutes'
     ORDER BY ks.ticker, ks.captured_at DESC
)
SELECT ap.polymarket_market_id,
       ap.kalshi_ticker,
       ap.event_id,
       ap.poly_category,
       ap.inverse_side,
       ap.poly_volume_24h,
       ap.poly_clob_token_ids,
       pn.yes_price       AS poly_yes_now,
       po.yes_price       AS poly_yes_old,
       pn.captured_at     AS poly_now_at,
       kn.yes_ask_cents   AS kalshi_yes_now_cents,
       ko.yes_ask_cents   AS kalshi_yes_old_cents,
       kn.volume_24h_contracts AS kalshi_vol_contracts,
       kn.last_price_cents     AS kalshi_last_cents
  FROM active_pairs ap
  LEFT JOIN poly_now    pn ON pn.market_id = ap.poly_market_uuid
  LEFT JOIN poly_old    po ON po.market_id = ap.poly_market_uuid
  LEFT JOIN kalshi_now  kn ON kn.ticker    = ap.kalshi_ticker
  LEFT JOIN kalshi_old  ko ON ko.ticker    = ap.kalshi_ticker
"""

_LOOKBACK_SEC = 600  # 10 min — соответствует INTERVAL в SQL


def _cents_to_price(cents: int | None) -> float | None:
    """Kalshi cents (1-99) → price (0.01-0.99)."""
    if cents is None:
        return None
    return float(cents) / 100.0


async def run_d45_scan() -> dict[str, Any]:
    rows = await fetch(_PAIRED_SNAPSHOTS_SQL)
    if not rows:
        logger.info("d45_scan_no_pairs")
        return {"pairs": 0, "signals": 0, "inserted": 0}

    repo = get_detector_signal_repository()
    now = datetime.now(UTC)
    n_signals = n_inserted = 0

    for r in rows:
        # Skip pairs missing any snapshot (typical when Kalshi pipeline not running)
        if r.get("poly_yes_now") is None or r.get("poly_yes_old") is None:
            continue
        if r.get("kalshi_yes_now_cents") is None or r.get("kalshi_yes_old_cents") is None:
            continue

        # Kalshi volume — convert contracts × last_price_cents/100 to USD
        kalshi_vol_contracts = int(r.get("kalshi_vol_contracts") or 0)
        kalshi_last_cents = int(r.get("kalshi_last_cents") or 50)
        kalshi_volume_usd = kalshi_vol_contracts * (kalshi_last_cents / 100.0)

        pair = d45_lead_lag_cross_venue.LeadLagPair(
            polymarket_id=r["polymarket_market_id"],
            kalshi_ticker=r["kalshi_ticker"],
            event_id=r.get("event_id"),
            category=r.get("poly_category"),
            inverse_side=bool(r.get("inverse_side")),
            poly_yes_old=float(r["poly_yes_old"]),
            poly_yes_now=float(r["poly_yes_now"]),
            kalshi_yes_old=_cents_to_price(r.get("kalshi_yes_old_cents")),
            kalshi_yes_now=_cents_to_price(r.get("kalshi_yes_now_cents")),
            poly_volume_24h=float(r.get("poly_volume_24h") or 0),
            kalshi_volume_24h_usd=kalshi_volume_usd,
            lookback_sec=_LOOKBACK_SEC,
            snapshot_at=now,
            poly_clob_token_ids=r.get("poly_clob_token_ids"),
        )

        sig = d45_lead_lag_cross_venue.detect(pair)
        if sig is None:
            continue

        # Idempotency: 1-hour bucket per (poly_market_id, kalshi_ticker).
        # Scan каждые 5 мин, но эмитим максимум 1 signal/hour/pair (lag окно 5-30 min)
        bucket_key = f"{r['polymarket_market_id']}:{r['kalshi_ticker']}"
        tx_hash = bucket_tx_hash(bucket_id=bucket_key, ts=now, bucket_hours=1, namespace="d45")
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
        n_signals += 1
        try:
            if await repo.insert(sig):
                n_inserted += 1
        except Exception as exc:
            logger.warning("d45_scan_insert_failed", error=str(exc), market=sig.market_id)

    logger.info(
        "d45_scan_completed",
        pairs=len(rows),
        signals=n_signals,
        inserted=n_inserted,
    )
    return {"pairs": len(rows), "signals": n_signals, "inserted": n_inserted}
