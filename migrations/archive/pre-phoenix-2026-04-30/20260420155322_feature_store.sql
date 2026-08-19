-- feature_store: Unified source of truth for backtest + live scoring
-- Rows come from either HF historical corpus (source='hf') or live oracle_snapshots (source='live').
-- UNIQUE (market_id, snapshot_at, side, features_version) enables idempotent UPSERT.

CREATE TABLE IF NOT EXISTS feature_store (
    id BIGSERIAL PRIMARY KEY,

    -- Identity
    market_id TEXT NOT NULL,
    condition_id TEXT,
    event_id UUID,
    snapshot_at TIMESTAMPTZ NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('YES','NO')),

    -- Market metadata
    category TEXT,
    question TEXT,
    horizon_days NUMERIC,

    -- Prices
    market_price NUMERIC,
    entry_price NUMERIC,
    vwap_7d NUMERIC,
    volume_24h NUMERIC,

    -- Structural features (always available)
    maker_direction_raw NUMERIC,
    taker_imbalance_raw NUMERIC,
    whale_tx_count INTEGER DEFAULT 0,

    -- Oracle input components (may be NULL on HF rows)
    whale_contribution_raw NUMERIC,
    news_contribution_raw NUMERIC,
    sm_fresh_raw NUMERIC,
    sm_cluster_raw NUMERIC,
    sm_registry_raw NUMERIC,
    oi_factor_raw NUMERIC,
    concentration_factor_raw NUMERIC,
    micro_factor_raw NUMERIC,

    -- Oracle outputs (filled by Phase 2 scoring backfill, NULL for now)
    oracle_score_prod NUMERIC,
    oracle_score_experimental NUMERIC,
    divergence_prod NUMERIC,
    divergence_experimental NUMERIC,
    z_posterior_prod NUMERIC,
    z_posterior_experimental NUMERIC,

    config_hash_prod TEXT,
    config_hash_experimental TEXT,

    -- Signal availability — explicit list of non-NULL component columns
    components_available TEXT[] NOT NULL DEFAULT '{}',

    -- Outcome
    outcome TEXT,
    win INTEGER CHECK (win IN (0, 1)),
    resolved_at TIMESTAMPTZ,

    -- Provenance
    source TEXT NOT NULL CHECK (source IN ('hf','live','hf_enriched')),
    features_version TEXT NOT NULL DEFAULT '1.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT feature_store_unique UNIQUE (market_id, snapshot_at, side, features_version)
);

CREATE INDEX IF NOT EXISTS idx_fs_snapshot_at ON feature_store (snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_fs_category_price ON feature_store (category, market_price);
CREATE INDEX IF NOT EXISTS idx_fs_source_outcome ON feature_store (source, outcome);
CREATE INDEX IF NOT EXISTS idx_fs_live_pending ON feature_store (snapshot_at DESC)
    WHERE source = 'live' AND outcome = 'pending';
CREATE INDEX IF NOT EXISTS idx_fs_resolved ON feature_store (resolved_at DESC)
    WHERE outcome IN ('yes','no');

COMMENT ON TABLE feature_store IS
  'Unified feature store: HF historical + live oracle_snapshots. Phase 1 of backtest refactor.';
