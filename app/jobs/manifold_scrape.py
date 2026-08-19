"""manifold-scrape job — every 15 min.

Pull active BINARY Manifold markets, upsert в manifold_markets,
capture probability snapshot.

Phase 1 strategy (PDF §11): collect raw signal data. Cross-venue matching
к canonical_events происходит отдельным cron (Phase 2 — embedding matcher).

Cron schedule: every 15 min (matches kalshi-orderbook-sync cadence для
parity цикл cross-venue snapshots).

NO POLYMARKET_PROXY: Manifold = public read-only API, не нарушает
CLAUDE.md rule про proxy execution-only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.services.database import execute, executemany
from app.services.manifold_service import get_manifold_client

logger = structlog.get_logger()


# Minimum volume threshold (mana, не USD — Manifold mana ≈ играю-валюта)
# 100 mana = ~$1 in cash markets, для отсева noise markets
MIN_VOLUME_24H_MANA = 50.0


_UPSERT_MARKET_SQL = """
INSERT INTO manifold_markets (
    id, slug, creator_username, question, description, outcome_type,
    is_resolved, resolution, resolution_probability, probability,
    volume_24h, volume_total, unique_bettors,
    close_time, created_time, last_updated_time, url, raw, updated_at
)
VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10,
    $11, $12, $13,
    $14, $15, $16, $17, $18::jsonb, now()
)
ON CONFLICT (id) DO UPDATE SET
    slug                  = EXCLUDED.slug,
    creator_username      = EXCLUDED.creator_username,
    question              = EXCLUDED.question,
    description           = EXCLUDED.description,
    outcome_type          = EXCLUDED.outcome_type,
    is_resolved           = EXCLUDED.is_resolved,
    resolution            = EXCLUDED.resolution,
    resolution_probability= EXCLUDED.resolution_probability,
    probability           = EXCLUDED.probability,
    volume_24h            = EXCLUDED.volume_24h,
    volume_total          = EXCLUDED.volume_total,
    unique_bettors        = EXCLUDED.unique_bettors,
    close_time            = EXCLUDED.close_time,
    created_time          = EXCLUDED.created_time,
    last_updated_time     = EXCLUDED.last_updated_time,
    url                   = EXCLUDED.url,
    raw                   = EXCLUDED.raw,
    updated_at            = now()
"""

_INSERT_SNAPSHOT_SQL = """
INSERT INTO manifold_price_snapshots (market_id, captured_at, probability, volume_24h, unique_bettors)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (market_id, captured_at) DO NOTHING
"""


def _ts_to_dt(ms: int | None) -> datetime | None:
    """Manifold timestamps = milliseconds since epoch."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _build_upsert_row(m: dict[str, Any]) -> tuple:
    return (
        m["id"],
        m["slug"],
        m.get("creatorUsername"),
        m["question"],
        m.get("textDescription") or m.get("description") or None,
        m.get("outcomeType"),
        bool(m.get("isResolved")),
        m.get("resolution"),
        m.get("resolutionProbability"),
        m.get("probability"),
        m.get("volume24Hours"),
        m.get("volume"),
        m.get("uniqueBettorCount"),
        _ts_to_dt(m.get("closeTime")),
        _ts_to_dt(m.get("createdTime")),
        _ts_to_dt(m.get("lastUpdatedTime") or m.get("lastBetTime")),
        m.get("url"),
        json.dumps(m),
    )


async def run_manifold_scrape() -> dict[str, Any]:
    """Pull active BINARY Manifold markets, upsert + snapshot."""
    client = get_manifold_client()
    markets = await client.list_active_binary_markets(
        max_pages=5,
        min_volume_24h=MIN_VOLUME_24H_MANA,
    )

    if not markets:
        logger.info("manifold_scrape_no_markets")
        return {"markets": 0, "upserted": 0, "snapshots": 0}

    now = datetime.now(UTC)
    n_upserted = 0
    snapshot_rows: list[tuple] = []

    for m in markets:
        try:
            row = _build_upsert_row(m)
            await execute(_UPSERT_MARKET_SQL, *row)
            n_upserted += 1
        except Exception as exc:
            logger.warning(
                "manifold_upsert_failed",
                market_id=m.get("id"),
                error=str(exc),
            )
            continue

        prob = m.get("probability")
        if prob is None:
            continue
        snapshot_rows.append(
            (
                m["id"],
                now,
                prob,
                m.get("volume24Hours"),
                m.get("uniqueBettorCount"),
            )
        )

    n_snapshots = 0
    if snapshot_rows:
        try:
            await executemany(_INSERT_SNAPSHOT_SQL, snapshot_rows)
            n_snapshots = len(snapshot_rows)
        except Exception as exc:
            logger.warning("manifold_snapshot_executemany_failed", error=str(exc))

    logger.info(
        "manifold_scrape_done",
        markets=len(markets),
        upserted=n_upserted,
        snapshots=n_snapshots,
    )
    return {"markets": len(markets), "upserted": n_upserted, "snapshots": n_snapshots}
