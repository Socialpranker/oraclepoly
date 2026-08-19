-- 20260520171823_manifold_markets.sql
-- Description: Manifold Markets ingestion для cross-venue information edge (PDF §11).
--
-- Manifold = play-money + small cash prediction market. PDF говорит:
--   "Топовый источник information edge — early signals до миграции в Polymarket"
--
-- Phase 1: pull active binary markets, capture price snapshots каждые 15 min.
-- Phase 2: matcher → canonical_events через slug/title similarity.
-- Phase 3: D45-like detector compares Polymarket↔Manifold convergence.
--
-- Public API, no auth required (https://docs.manifold.markets/api).

-- ─────────────────────────────────────────────────────────────────────
-- manifold_markets — mirror к Manifold REST API GET /v0/markets
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.manifold_markets (
    id                  text PRIMARY KEY,           -- Manifold market id
    slug                text NOT NULL UNIQUE,
    creator_username    text,
    question            text NOT NULL,
    description         text,
    outcome_type        text,                       -- BINARY/MULTIPLE_CHOICE/PSEUDO_NUMERIC/etc
    is_resolved         boolean NOT NULL DEFAULT false,
    resolution          text,                       -- 'YES'|'NO'|'MKT'|'CANCEL' etc
    resolution_probability numeric(5, 4),
    probability         numeric(5, 4),              -- current YES prob (0..1)
    volume_24h          numeric(20, 6),
    volume_total        numeric(20, 6),
    unique_bettors      integer,
    close_time          timestamptz,
    created_time        timestamptz,
    last_updated_time   timestamptz,
    url                 text,
    raw                 jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_manifold_markets_close_time
    ON public.manifold_markets (close_time DESC)
    WHERE is_resolved = false;

CREATE INDEX IF NOT EXISTS idx_manifold_markets_volume_24h
    ON public.manifold_markets (volume_24h DESC NULLS LAST)
    WHERE is_resolved = false;

CREATE INDEX IF NOT EXISTS idx_manifold_markets_outcome_type
    ON public.manifold_markets (outcome_type)
    WHERE is_resolved = false;

COMMENT ON TABLE public.manifold_markets IS
    'Manifold Markets mirror — populated by manifold-scrape cron (every 15 min). Public API.';

-- ─────────────────────────────────────────────────────────────────────
-- manifold_price_snapshots — time-series probability
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.manifold_price_snapshots (
    market_id           text NOT NULL,
    captured_at         timestamptz NOT NULL,
    probability         numeric(5, 4),
    volume_24h          numeric(20, 6),
    unique_bettors      integer,
    PRIMARY KEY (market_id, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_manifold_snapshots_captured
    ON public.manifold_price_snapshots (captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_manifold_snapshots_market
    ON public.manifold_price_snapshots (market_id, captured_at DESC);

COMMENT ON TABLE public.manifold_price_snapshots IS
    'Time-series probability для Manifold markets (PDF §11 information edge tracking)';

-- Rollback:
-- DROP TABLE IF EXISTS public.manifold_price_snapshots;
-- DROP TABLE IF EXISTS public.manifold_markets;
