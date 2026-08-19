"""CanonicalEventRepository — cross-venue event taxonomy.

Maps abstract events ("2026 Super Bowl winner") к venue-specific markets
(Polymarket condition_id, Kalshi ticker, Manifold slug, Betfair selection).

Используется:
- D45 lead-lag (Polymarket↔Kalshi paired snapshots)
- A-4 resolution arb (cross-venue resolution timing)
- S-1 multi-venue arbitrage
- Manifold signal scraper (information edge per PDF §11)

См. migrations/20260520165016_canonical_events_taxonomy.sql
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()


@dataclass(frozen=True)
class CanonicalEvent:
    """Abstract cross-venue event."""

    id: str  # UUID as str
    slug: str
    title: str
    category: str
    description: str | None = None
    resolution_criterion: str | None = None
    resolution_source: str | None = None
    expected_resolves_at: datetime | None = None
    status: str = "active"
    resolved_at: datetime | None = None
    resolved_outcome: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class VenueMapping:
    """Per-venue market mapping для canonical event."""

    canonical_event_id: str
    venue: str  # 'polymarket'|'kalshi'|'manifold'|'betfair'|'smarkets'|'limitless'|'drift_predict'
    venue_market_id: str
    venue_market_url: str | None = None
    side_semantics: str = "direct"  # 'direct'|'inverse'
    confidence_score: float = 1.0
    matched_by: str = "manual"
    notes: str | None = None


class CanonicalEventRepository:
    """CRUD + JOIN helpers для canonical_events + venue_market_mappings."""

    async def upsert_event(
        self,
        *,
        slug: str,
        title: str,
        category: str,
        description: str | None = None,
        resolution_criterion: str | None = None,
        resolution_source: str | None = None,
        expected_resolves_at: datetime | None = None,
        notes: str | None = None,
    ) -> str:
        """Insert или update by slug. Returns canonical_event_id (uuid str)."""
        row = await fetchrow(
            """
            INSERT INTO canonical_events (
                slug, title, category, description,
                resolution_criterion, resolution_source,
                expected_resolves_at, notes
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (slug) DO UPDATE SET
                title = EXCLUDED.title,
                category = EXCLUDED.category,
                description = COALESCE(EXCLUDED.description, canonical_events.description),
                resolution_criterion = COALESCE(EXCLUDED.resolution_criterion, canonical_events.resolution_criterion),
                resolution_source = COALESCE(EXCLUDED.resolution_source, canonical_events.resolution_source),
                expected_resolves_at = COALESCE(EXCLUDED.expected_resolves_at, canonical_events.expected_resolves_at),
                notes = COALESCE(EXCLUDED.notes, canonical_events.notes),
                updated_at = now()
            RETURNING id
            """,
            slug,
            title,
            category,
            description,
            resolution_criterion,
            resolution_source,
            expected_resolves_at,
            notes,
        )
        return str(row["id"])

    async def upsert_mapping(
        self,
        *,
        canonical_event_id: str,
        venue: str,
        venue_market_id: str,
        venue_market_url: str | None = None,
        side_semantics: str = "direct",
        confidence_score: float = 1.0,
        matched_by: str = "manual",
        notes: str | None = None,
    ) -> None:
        """Upsert venue mapping. Composite key (canonical_event_id, venue, venue_market_id)."""
        await execute(
            """
            INSERT INTO venue_market_mappings (
                canonical_event_id, venue, venue_market_id, venue_market_url,
                side_semantics, confidence_score, matched_by, notes
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (canonical_event_id, venue, venue_market_id) DO UPDATE SET
                venue_market_url = COALESCE(EXCLUDED.venue_market_url, venue_market_mappings.venue_market_url),
                side_semantics = EXCLUDED.side_semantics,
                confidence_score = EXCLUDED.confidence_score,
                matched_by = EXCLUDED.matched_by,
                notes = COALESCE(EXCLUDED.notes, venue_market_mappings.notes),
                expired_at = NULL
            """,
            canonical_event_id,
            venue,
            venue_market_id,
            venue_market_url,
            side_semantics,
            confidence_score,
            matched_by,
            notes,
        )

    async def get_by_slug(self, slug: str) -> dict[str, Any] | None:
        row = await fetchrow(
            "SELECT * FROM canonical_events WHERE slug = $1",
            slug,
        )
        return dict(row) if row else None

    async def get_mappings(self, canonical_event_id: str) -> list[dict[str, Any]]:
        """Все non-expired venue mappings для canonical event."""
        rows = await fetch(
            """
            SELECT venue, venue_market_id, venue_market_url,
                   side_semantics, confidence_score, matched_by
              FROM venue_market_mappings
             WHERE canonical_event_id = $1
               AND expired_at IS NULL
             ORDER BY venue, confidence_score DESC
            """,
            canonical_event_id,
        )
        return [dict(r) for r in rows]

    async def find_paired_markets(
        self,
        venue_a: str,
        venue_b: str,
        *,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Возвращает пары (event, venue_a_market, venue_b_market) для cross-venue scans.

        Используется D45 (Polymarket+Kalshi), Manifold scraper (Polymarket+Manifold).
        """
        params: list[Any] = [venue_a, venue_b]
        category_filter = ""
        if category is not None:
            category_filter = " AND ce.category = $3"
            params.append(category)

        rows = await fetch(
            f"""
            SELECT ce.id              AS canonical_event_id,
                   ce.slug,
                   ce.title,
                   ce.category,
                   ma.venue_market_id AS venue_a_market_id,
                   ma.side_semantics  AS venue_a_side,
                   mb.venue_market_id AS venue_b_market_id,
                   mb.side_semantics  AS venue_b_side,
                   (ma.side_semantics != mb.side_semantics) AS is_inverse
              FROM canonical_events ce
              JOIN venue_market_mappings ma
                ON ma.canonical_event_id = ce.id
               AND ma.venue = $1
               AND ma.expired_at IS NULL
              JOIN venue_market_mappings mb
                ON mb.canonical_event_id = ce.id
               AND mb.venue = $2
               AND mb.expired_at IS NULL
             WHERE ce.status = 'active'{category_filter}
             ORDER BY ce.expected_resolves_at NULLS LAST
            """,
            *params,
        )
        return [dict(r) for r in rows]

    async def expire_mapping(
        self,
        *,
        canonical_event_id: str,
        venue: str,
        venue_market_id: str,
    ) -> None:
        """Soft-delete: mark as expired (не удаляем для audit)."""
        await execute(
            """
            UPDATE venue_market_mappings
               SET expired_at = now()
             WHERE canonical_event_id = $1 AND venue = $2 AND venue_market_id = $3
            """,
            canonical_event_id,
            venue,
            venue_market_id,
        )

    async def resolve_event(
        self,
        slug: str,
        outcome: str,
    ) -> None:
        """Mark event resolved."""
        await execute(
            """
            UPDATE canonical_events
               SET status = 'resolved', resolved_at = now(), resolved_outcome = $2, updated_at = now()
             WHERE slug = $1
            """,
            slug,
            outcome,
        )


def get_canonical_event_repository() -> CanonicalEventRepository:
    return CanonicalEventRepository()
