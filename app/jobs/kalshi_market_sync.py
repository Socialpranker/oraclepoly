"""Kalshi market sync — Phase 1.0 (every 30 min).

Pull all active Kalshi markets, upsert in `kalshi_markets`.

Source: research/kalshi-integration-2026-05/prd.md, Phase 1.0.

Workflow:
1. Paginate `/markets?status=open` → list (typically <5K)
2. Upsert each row to `kalshi_markets` by ticker (PRIMARY KEY)
3. Mark `is_active=false` for tickers absent from current sweep (closed markets)

Phase 1 categories filter: Sports, Economics, Politics (allowed cohort).
Other categories ingested too (для future expansion), но не используются в D40.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.services.database import execute, fetchval
from app.services.kalshi_service import KalshiAuthError, KalshiClient, alert_kalshi_auth_failure

logger = structlog.get_logger()


_UPSERT_SQL = """
INSERT INTO public.kalshi_markets (
    ticker, event_ticker, series_ticker, title, subtitle, category,
    yes_bid_cents, yes_ask_cents, no_bid_cents, no_ask_cents,
    last_price_cents, volume_24h_contracts, open_interest,
    open_time, close_time, expiration_time,
    is_active, raw, updated_at
)
VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10,
    $11, $12, $13,
    $14, $15, $16,
    true, $17, now()
)
ON CONFLICT (ticker) DO UPDATE SET
    event_ticker         = EXCLUDED.event_ticker,
    series_ticker        = EXCLUDED.series_ticker,
    title                = EXCLUDED.title,
    subtitle             = EXCLUDED.subtitle,
    category             = EXCLUDED.category,
    yes_bid_cents        = EXCLUDED.yes_bid_cents,
    yes_ask_cents        = EXCLUDED.yes_ask_cents,
    no_bid_cents         = EXCLUDED.no_bid_cents,
    no_ask_cents         = EXCLUDED.no_ask_cents,
    last_price_cents     = EXCLUDED.last_price_cents,
    volume_24h_contracts = EXCLUDED.volume_24h_contracts,
    open_interest        = EXCLUDED.open_interest,
    open_time            = EXCLUDED.open_time,
    close_time           = EXCLUDED.close_time,
    expiration_time      = EXCLUDED.expiration_time,
    is_active            = true,
    raw                  = EXCLUDED.raw,
    updated_at           = now()
"""

_DEACTIVATE_STALE_SQL = """
UPDATE public.kalshi_markets
   SET is_active = false, updated_at = now()
 WHERE is_active = true
   AND ticker NOT IN (SELECT unnest($1::text[]))
"""


def _coerce_int(v: Any) -> int | None:
    """Defensive cast — Kalshi sometimes returns floats для contract counts."""
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _coerce_smallint(v: Any) -> int | None:
    """Cast to smallint (1-99 для price, могут быть None)."""
    iv = _coerce_int(v)
    if iv is None:
        return None
    return max(-32768, min(32767, iv))


def _parse_ts(v: Any) -> datetime | None:
    """Parse ISO timestamp from Kalshi response."""
    if not v:
        return None
    try:
        # Kalshi sends Z suffix; fromisoformat handles +00:00 in py3.11+
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def run_kalshi_market_sync() -> dict[str, Any]:
    """Pull all open Kalshi markets, upsert. Returns stats.

    Если private key недоступен или Kalshi 4xx — лог + early return (нет crash).
    """
    started = datetime.now(UTC)

    try:
        async with KalshiClient() as client:
            markets = await client.list_markets_all(status="open", max_pages=50, page_limit=200)
    except KalshiAuthError as exc:
        logger.warning("kalshi_market_sync_no_key", error=str(exc))
        await alert_kalshi_auth_failure("kalshi_market_sync", str(exc))
        return {"status": "skipped", "reason": "no_private_key", "count": 0}
    except Exception as exc:
        logger.error("kalshi_market_sync_fetch_failed", error=str(exc), error_type=type(exc).__name__)
        return {"status": "error", "reason": str(exc), "count": 0}

    if not markets:
        logger.info("kalshi_market_sync_empty")
        return {"status": "ok", "count": 0, "deactivated": 0}

    n_upserted = 0
    for m in markets:
        ticker = m.get("ticker")
        event_ticker = m.get("event_ticker")
        if not ticker or not event_ticker:
            continue
        try:
            await execute(
                _UPSERT_SQL,
                ticker,
                event_ticker,
                m.get("series_ticker"),
                m.get("title") or m.get("yes_sub_title") or "",
                m.get("subtitle"),
                m.get("category"),
                _coerce_smallint(m.get("yes_bid")),
                _coerce_smallint(m.get("yes_ask")),
                _coerce_smallint(m.get("no_bid")),
                _coerce_smallint(m.get("no_ask")),
                _coerce_smallint(m.get("last_price")),
                _coerce_int(m.get("volume_24h")),
                _coerce_int(m.get("open_interest")),
                _parse_ts(m.get("open_time")),
                _parse_ts(m.get("close_time")),
                _parse_ts(m.get("expiration_time")),
                json.dumps(m),
            )
            n_upserted += 1
        except Exception as exc:
            logger.warning("kalshi_market_upsert_failed", ticker=ticker, error=str(exc))

    # Deactivate markets which disappeared from "open" set (closed/settled)
    active_tickers = [m["ticker"] for m in markets if m.get("ticker")]
    deactivated = 0
    if active_tickers:
        try:
            result = await execute(_DEACTIVATE_STALE_SQL, active_tickers)
            # Postgres returns "UPDATE N" string
            if isinstance(result, str) and result.startswith("UPDATE "):
                deactivated = int(result.split()[1])
        except Exception as exc:
            logger.warning("kalshi_market_deactivate_failed", error=str(exc))

    elapsed = (datetime.now(UTC) - started).total_seconds()
    total_active = await fetchval("SELECT COUNT(*) FROM public.kalshi_markets WHERE is_active = true")

    logger.info(
        "kalshi_market_sync_completed",
        upserted=n_upserted,
        deactivated=deactivated,
        total_active=total_active,
        elapsed_sec=round(elapsed, 2),
    )
    return {
        "status": "ok",
        "count": n_upserted,
        "deactivated": deactivated,
        "total_active": total_active,
        "elapsed_sec": round(elapsed, 2),
    }
