-- 20260520165016_canonical_events_taxonomy.sql
-- Description: Canonical event taxonomy для cross-venue mapping (PDF §14.b).
--
-- Архитектура:
--   canonical_events — abstract event (e.g. "2026 NFL Super Bowl winner")
--   venue_market_mappings — per-venue market_id mapping для canonical event
--
-- Используется S-1 lead-lag (D45), A-4 resolution arb, и любым cross-venue
-- detector'ом в будущем. Без canonical event_id нельзя сопоставить рынки
-- между Polymarket/Kalshi/Manifold/Betfair автоматически.
--
-- market_pair_matches table (kalshi-integration Phase 1.1) — это subset
-- этой схемы (только Polymarket↔Kalshi через embeddings). Canonical_events
-- даёт более общую модель для top-30 events с manual mapping.

-- ─────────────────────────────────────────────────────────────────────
-- canonical_events — abstract events
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.canonical_events (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                  text NOT NULL UNIQUE,         -- "2026-nfl-super-bowl-winner"
    title                 text NOT NULL,                -- human-readable
    description           text,                         -- context для disambiguation
    category              text NOT NULL,                -- politics/sports/economics/crypto/macro
    resolution_criterion  text,                         -- formal: "winning team в final game"
    resolution_source     text,                         -- "official NFL announcement"
    expected_resolves_at  timestamptz,                  -- approximate
    status                text NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'resolved', 'cancelled', 'disputed')),
    resolved_at           timestamptz,
    resolved_outcome      text,                         -- "Chiefs" / "Trump" / numeric
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    notes                 text
);

CREATE INDEX IF NOT EXISTS idx_canonical_events_category
    ON public.canonical_events (category)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_canonical_events_status_resolves
    ON public.canonical_events (status, expected_resolves_at)
    WHERE status = 'active';

COMMENT ON TABLE public.canonical_events IS
    'Abstract cross-venue events. Each event has 0..N venue-specific markets через venue_market_mappings.';

-- ─────────────────────────────────────────────────────────────────────
-- venue_market_mappings — per-venue market mapping
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.venue_market_mappings (
    canonical_event_id   uuid NOT NULL REFERENCES public.canonical_events(id) ON DELETE CASCADE,
    venue                text NOT NULL CHECK (venue IN ('polymarket','kalshi','manifold','betfair','smarkets','limitless','drift_predict')),
    venue_market_id      text NOT NULL,                -- venue-specific identifier
    venue_market_url     text,                         -- direct link для debugging
    side_semantics       text NOT NULL DEFAULT 'direct'
                          CHECK (side_semantics IN ('direct', 'inverse')),
    -- direct: venue YES == canonical "X happens"
    -- inverse: venue YES == canonical "X does NOT happen" (need swap)
    confidence_score     numeric(4, 3) NOT NULL DEFAULT 1.0
                          CHECK (confidence_score >= 0 AND confidence_score <= 1),
    -- 1.0 = manual verified, <1.0 = auto-matched (cosine similarity)
    matched_by           text,                         -- 'manual'|'embedding'|'llm'
    matched_at           timestamptz NOT NULL DEFAULT now(),
    expired_at           timestamptz,                  -- когда market закрылся / mapping устарел
    notes                text,
    PRIMARY KEY (canonical_event_id, venue, venue_market_id)
);

CREATE INDEX IF NOT EXISTS idx_venue_mappings_canonical
    ON public.venue_market_mappings (canonical_event_id)
    WHERE expired_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_venue_mappings_venue_market
    ON public.venue_market_mappings (venue, venue_market_id)
    WHERE expired_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_venue_mappings_venue_category
    ON public.venue_market_mappings (venue)
    INCLUDE (canonical_event_id)
    WHERE expired_at IS NULL;

COMMENT ON TABLE public.venue_market_mappings IS
    'Maps canonical event ↔ venue-specific market. Phase 1: manual seed top-30. Phase 2: auto-match через embeddings.';

-- Rollback:
-- DROP TABLE IF EXISTS public.venue_market_mappings;
-- DROP TABLE IF EXISTS public.canonical_events;
