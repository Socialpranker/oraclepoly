"""manifold-bets-scrape job — every 30 min.

Incremental pull of individual bets для tracked Manifold markets (те что
сматчены с Polymarket через venue_market_mappings).

Аудит 2026-05-21 C.6 gap #3, приоритет 5:
PDF §11 information edge — knowledge of timing/size индивидуальных bets
даёт lead-lag сигнал 4-12h раньше Polymarket-движения. Питает cross-venue
Insights (tier2 manifold_divergence). Bet-level гранулярность поверх
aggregated probability snapshots.
(D46-детектор снесён 2026-06-20 — MV=0 by-design, см. detector_triage. Manifold
данные остаются для Insights-вкладки.)

Strategy:
1. Берём manifold markets из venue_market_mappings(venue='manifold') —
   только те что сматчены с Polymarket-парами (релевантные для cross-venue).
2. Для каждого — incremental pull через manifold_service.list_bets_since().
3. Cursor = последний `bet_id` в нашей БД per market.

NO POLYMARKET_PROXY: Manifold public API.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.services.database import executemany, fetch, fetchval
from app.services.manifold_service import get_manifold_client

logger = structlog.get_logger()


# Safety cap per market на одно run чтобы не съесть rate-limit на одном market.
MAX_PAGES_PER_MARKET = 5  # 5 × 1000 = 5K bets per market per run


_FETCH_TRACKED_MARKETS_SQL = """
SELECT DISTINCT venue_market_id AS manifold_id
FROM venue_market_mappings
WHERE venue = 'manifold'
"""

_FETCH_LATEST_BET_ID_SQL = """
SELECT bet_id
FROM manifold_bets
WHERE manifold_market_id = $1
ORDER BY created_time DESC
LIMIT 1
"""

_INSERT_BET_SQL = """
INSERT INTO manifold_bets (
    bet_id, manifold_market_id, user_id, amount, outcome,
    prob_before, prob_after, shares, is_filled, is_redemption, fees,
    created_time, raw
)
VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10, $11,
    $12, $13::jsonb
)
ON CONFLICT (bet_id) DO NOTHING
"""


def _ts_to_dt(ms: int | None) -> datetime | None:
    """Manifold timestamps = milliseconds since epoch."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _build_bet_row(market_id: str, bet: dict[str, Any]) -> tuple:
    return (
        bet["id"],
        market_id,
        bet.get("userId"),
        bet.get("amount"),
        bet.get("outcome"),
        bet.get("probBefore"),
        bet.get("probAfter"),
        bet.get("shares"),
        bool(bet.get("isFilled")) if "isFilled" in bet else None,
        bool(bet.get("isRedemption")) if "isRedemption" in bet else None,
        bet.get("fees"),
        _ts_to_dt(bet.get("createdTime")),
        json.dumps(bet),
    )


async def run_manifold_bets_scrape() -> dict[str, Any]:
    """Incremental pull bets for all tracked Manifold markets."""
    tracked = await fetch(_FETCH_TRACKED_MARKETS_SQL)
    if not tracked:
        logger.info("manifold_bets_scrape_no_tracked_markets")
        return {"markets": 0, "inserted": 0}

    client = get_manifold_client()
    n_inserted = 0
    n_markets_processed = 0

    for row in tracked:
        manifold_id = row["manifold_id"]
        try:
            last_bet_id = await fetchval(_FETCH_LATEST_BET_ID_SQL, manifold_id)
            new_bets = await client.list_bets_since(
                manifold_id,
                since_bet_id=last_bet_id,
                max_pages=MAX_PAGES_PER_MARKET,
            )
        except Exception as exc:
            logger.warning(
                "manifold_bets_fetch_failed",
                manifold_id=manifold_id,
                error=str(exc),
            )
            continue

        n_markets_processed += 1
        if not new_bets:
            continue

        rows = [_build_bet_row(manifold_id, bet) for bet in new_bets if bet.get("id")]
        if not rows:
            continue

        try:
            await executemany(_INSERT_BET_SQL, rows)
            n_inserted += len(rows)
        except Exception as exc:
            logger.warning(
                "manifold_bets_insert_failed",
                manifold_id=manifold_id,
                n_bets=len(rows),
                error=str(exc),
            )

    logger.info(
        "manifold_bets_scrape_done",
        tracked_markets=len(tracked),
        processed=n_markets_processed,
        inserted=n_inserted,
    )
    return {
        "markets": len(tracked),
        "processed": n_markets_processed,
        "inserted": n_inserted,
    }
