"""Kalshi orderbook sync — Phase 1.2 (every 5 min).

Bulk-fetch orderbooks для tickers из активных market_pair_matches.
Write snapshots в kalshi_orderbook_snapshots для time-series accumulation.

Kalshi bulk endpoint `/markets/orderbooks` capped at 100 tickers per request,
поэтому chunked.

Source: research/kalshi-integration-2026-05/prd.md Phase 1.2
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.services.database import execute, fetch
from app.services.kalshi_service import KalshiAuthError, KalshiClient, alert_kalshi_auth_failure

logger = structlog.get_logger()


_TRACKED_TICKERS_SQL = """
SELECT DISTINCT kalshi_ticker
  FROM public.market_pair_matches
 WHERE expired_at IS NULL
   AND auto_matched = true
"""


_INSERT_SNAPSHOT_SQL = """
INSERT INTO public.kalshi_orderbook_snapshots (
    ticker, captured_at,
    yes_bid_cents, yes_bid_size,
    yes_ask_cents, yes_ask_size,
    no_bid_cents, no_bid_size,
    no_ask_cents, no_ask_size
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (ticker, captured_at) DO NOTHING
"""


KALSHI_BULK_CAP = 100


def _top_level(orders: list[Any] | None) -> tuple[int | None, int | None]:
    """Extract top price+size from Kalshi orderbook side array [[price_cents, size], ...]."""
    if not orders:
        return None, None
    top = orders[0]
    if not isinstance(top, list) or len(top) < 2:
        return None, None
    try:
        return int(top[0]), int(top[1])
    except (ValueError, TypeError):
        return None, None


def _parse_orderbook(ticker: str, ob: dict[str, Any]) -> dict[str, Any] | None:
    """Extract top-of-book values из Kalshi response. Returns None если orderbook пуст.

    Kalshi не отдаёт явные ask/bid. Структура:
        {"yes": [[price_cents, size], ...], "no": [[price_cents, size], ...]}
    Best bid YES = top of yes-array. Best ask YES = 100 - top_no_bid_cents (YES + NO = 100¢ invariant).
    """
    yes_orders = ob.get("yes") or []
    no_orders = ob.get("no") or []

    yes_bid_cents, yes_bid_size = _top_level(yes_orders)
    no_bid_cents, no_bid_size = _top_level(no_orders)

    # Derive YES ask from NO bid (100 - no_bid_cents): кто-то готов продать YES @ 100 - C
    yes_ask_cents = (100 - no_bid_cents) if no_bid_cents is not None else None
    yes_ask_size = no_bid_size  # размер совпадает с NO bid size
    # Derive NO ask from YES bid (100 - yes_bid_cents)
    no_ask_cents = (100 - yes_bid_cents) if yes_bid_cents is not None else None
    no_ask_size = yes_bid_size

    return {
        "yes_bid_cents": yes_bid_cents,
        "yes_bid_size": yes_bid_size,
        "yes_ask_cents": yes_ask_cents,
        "yes_ask_size": yes_ask_size,
        "no_bid_cents": no_bid_cents,
        "no_bid_size": no_bid_size,
        "no_ask_cents": no_ask_cents,
        "no_ask_size": no_ask_size,
    }


async def run_kalshi_orderbook_sync() -> dict[str, Any]:
    """Bulk-fetch tracked tickers' orderbooks, write snapshots. Cron every 5 min."""
    started = datetime.now(UTC)

    rows = await fetch(_TRACKED_TICKERS_SQL)
    tickers = [r["kalshi_ticker"] for r in rows if r.get("kalshi_ticker")]
    if not tickers:
        logger.info("kalshi_orderbook_sync_no_tracked")
        return {"status": "skipped", "reason": "no_tracked_pairs", "count": 0}

    captured_at = datetime.now(UTC)
    n_written = 0
    n_failed_chunks = 0

    try:
        async with KalshiClient() as client:
            for chunk_start in range(0, len(tickers), KALSHI_BULK_CAP):
                chunk = tickers[chunk_start : chunk_start + KALSHI_BULK_CAP]
                try:
                    resp = await client.get_orderbooks_bulk(chunk)
                except Exception as exc:
                    n_failed_chunks += 1
                    logger.warning(
                        "kalshi_orderbook_chunk_failed",
                        chunk_start=chunk_start,
                        chunk_size=len(chunk),
                        error=str(exc),
                    )
                    continue

                # Response shape: {"orderbooks": [{"ticker": "...", "yes": [...], "no": [...]}, ...]}
                orderbooks = resp.get("orderbooks") or []
                for ob in orderbooks:
                    ticker = ob.get("ticker")
                    if not ticker:
                        continue
                    parsed = _parse_orderbook(ticker, ob)
                    if parsed is None:
                        continue
                    try:
                        await execute(
                            _INSERT_SNAPSHOT_SQL,
                            ticker,
                            captured_at,
                            parsed["yes_bid_cents"],
                            parsed["yes_bid_size"],
                            parsed["yes_ask_cents"],
                            parsed["yes_ask_size"],
                            parsed["no_bid_cents"],
                            parsed["no_bid_size"],
                            parsed["no_ask_cents"],
                            parsed["no_ask_size"],
                        )
                        n_written += 1
                    except Exception as exc:
                        logger.warning("kalshi_orderbook_insert_failed", ticker=ticker, error=str(exc))
    except KalshiAuthError as exc:
        logger.warning("kalshi_orderbook_sync_no_key", error=str(exc))
        await alert_kalshi_auth_failure("kalshi_orderbook_sync", str(exc))
        return {"status": "skipped", "reason": "no_private_key", "count": 0}

    elapsed = (datetime.now(UTC) - started).total_seconds()
    logger.info(
        "kalshi_orderbook_sync_completed",
        tracked=len(tickers),
        written=n_written,
        failed_chunks=n_failed_chunks,
        elapsed_sec=round(elapsed, 2),
    )
    return {
        "status": "ok",
        "tracked": len(tickers),
        "written": n_written,
        "failed_chunks": n_failed_chunks,
        "elapsed_sec": round(elapsed, 2),
    }
