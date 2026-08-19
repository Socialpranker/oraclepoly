"""D40 Kalshi cross-platform arb scan — Phase 1.2 (every 5 min).

Workflow:
1. JOIN markets × kalshi_markets × kalshi_orderbook_snapshots (latest) на pair_matches
2. Для каждой active non-inverse пары — d40.detect()
3. Emit signal в detector_signals если edge passes

Idempotency: per-pair 1h bucket (один сигнал на пару максимум раз в час).

Source: research/kalshi-integration-2026-05/prd.md Phase 1.2
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors import d40_kalshi_cross_platform as d40
from app.detectors.d40_kalshi_cross_platform import CrossPlatformPair
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch
from app.services.detector_helpers import bucket_tx_hash
from app.services.jsonb_utils import parse_jsonb

logger = structlog.get_logger()


# Лимит для безопасности: не больше N сигналов за запуск (anti-spam)
MAX_SIGNALS_PER_RUN = 25


_FETCH_PAIRS_SQL = """
WITH latest_kalshi_book AS (
    SELECT DISTINCT ON (ticker)
           ticker,
           yes_bid_cents, yes_ask_cents,
           no_bid_cents, no_ask_cents,
           captured_at
      FROM public.kalshi_orderbook_snapshots
     WHERE captured_at > now() - INTERVAL '15 minutes'
     ORDER BY ticker, captured_at DESC
)
SELECT
    m.polymarket_id              AS poly_id,
    m.event_id::text             AS event_id,
    m.category                   AS poly_category_raw,
    m.fee_schedule               AS poly_fee_schedule,
    m.current_price              AS poly_current_price,
    COALESCE(m.volume_24h, 0)    AS poly_volume_24h,
    m.clob_token_ids,
    km.ticker                    AS kalshi_ticker,
    km.category                  AS kalshi_category,
    km.volume_24h_contracts      AS kalshi_volume_contracts,
    lkb.yes_bid_cents            AS k_yes_bid,
    lkb.yes_ask_cents            AS k_yes_ask,
    lkb.no_bid_cents             AS k_no_bid,
    lkb.no_ask_cents             AS k_no_ask,
    mpm.inverse_side
  FROM public.market_pair_matches mpm
  JOIN public.markets m         ON m.polymarket_id = mpm.polymarket_market_id
                                AND m.is_active = true
                                AND m.resolved = false
  JOIN public.kalshi_markets km ON km.ticker = mpm.kalshi_ticker
                                AND km.is_active = true
  JOIN latest_kalshi_book lkb   ON lkb.ticker = km.ticker
 WHERE mpm.expired_at IS NULL
   AND mpm.auto_matched = true
"""


KALSHI_CONTRACTS_TO_USD = 0.5  # heuristic, same как в matcher


def _cents_to_float(c: int | None) -> float | None:
    if c is None:
        return None
    return float(c) / 100.0


def _build_pair(row: dict[str, Any]) -> CrossPlatformPair | None:
    """Convert DB row to CrossPlatformPair. Returns None если price data incomplete."""
    poly_yes = float(row["poly_current_price"]) if row.get("poly_current_price") is not None else None
    if poly_yes is None:
        return None
    # Polymarket markets table даёт нам только current_price = yes (mid).
    # Conservative: yes_ask ≈ yes_bid ≈ current_price (true ask requires CLOB book sync, есть в maker_direction_cache но пропускаю для Phase 1.2).
    # Будем использовать current_price как обе стороны (yes_ask = current, no_ask = 1 - current).
    poly_yes_ask = poly_yes
    poly_no_ask = round(1.0 - poly_yes, 4)

    return CrossPlatformPair(
        polymarket_id=str(row["poly_id"]),
        kalshi_ticker=str(row["kalshi_ticker"]),
        event_id=row.get("event_id"),
        category=str(row.get("kalshi_category") or row.get("poly_category_raw") or ""),
        polymarket_category_raw=str(row.get("poly_category_raw") or ""),
        inverse_side=bool(row.get("inverse_side") or False),
        poly_yes_ask=poly_yes_ask,
        poly_no_ask=poly_no_ask,
        kalshi_yes_ask=_cents_to_float(row.get("k_yes_ask")),
        kalshi_no_ask=_cents_to_float(row.get("k_no_ask")),
        poly_volume_24h=float(row.get("poly_volume_24h") or 0),
        kalshi_volume_24h_usd=float(row.get("kalshi_volume_contracts") or 0) * KALSHI_CONTRACTS_TO_USD,
        poly_clob_token_ids=row.get("clob_token_ids"),
        polymarket_fee_schedule=parse_jsonb(row.get("poly_fee_schedule")),
    )


async def run_d40_scan() -> dict[str, Any]:
    """Scan all active matched pairs, emit signals where edge ≥ threshold."""
    started = datetime.now(UTC)
    rows = await fetch(_FETCH_PAIRS_SQL)
    if not rows:
        logger.info("d40_scan_no_pairs")
        return {"status": "ok", "pairs": 0, "signals": 0, "inserted": 0}

    repo = get_detector_signal_repository()
    now = datetime.now(UTC)

    n_signals = 0
    n_inserted = 0
    n_skipped_inverse = 0
    n_skipped_no_edge = 0

    for row in rows:
        if n_signals >= MAX_SIGNALS_PER_RUN:
            logger.warning("d40_scan_hit_max_signals", max=MAX_SIGNALS_PER_RUN)
            break

        pair = _build_pair(dict(row))
        if pair is None:
            continue
        if pair.inverse_side:
            n_skipped_inverse += 1
            continue

        sig = d40.detect(pair)
        if sig is None:
            n_skipped_no_edge += 1
            continue

        # 1h dedup bucket per pair (poly_id|kalshi_ticker)
        bucket_id = f"{pair.polymarket_id}|{pair.kalshi_ticker}"
        tx_hash = bucket_tx_hash(bucket_id=bucket_id, ts=now, bucket_hours=1, namespace="d40")
        evidence = dict(sig.evidence)
        evidence["tx_hash"] = tx_hash
        from app.detectors.contract import Signal

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
            logger.warning("d40_scan_insert_failed", error=str(exc), pair=bucket_id)

    elapsed = (datetime.now(UTC) - started).total_seconds()
    logger.info(
        "d40_scan_completed",
        pairs=len(rows),
        signals=n_signals,
        inserted=n_inserted,
        skipped_inverse=n_skipped_inverse,
        skipped_no_edge=n_skipped_no_edge,
        elapsed_sec=round(elapsed, 2),
    )
    return {
        "status": "ok",
        "pairs": len(rows),
        "signals": n_signals,
        "inserted": n_inserted,
        "skipped_inverse": n_skipped_inverse,
        "skipped_no_edge": n_skipped_no_edge,
        "elapsed_sec": round(elapsed, 2),
    }
