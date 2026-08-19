-- migrations/174_sampling_markets.sql
CREATE TABLE IF NOT EXISTS sampling_markets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    condition_id TEXT NOT NULL,
    max_spread NUMERIC(6, 4),
    min_size NUMERIC(12, 2),
    reward_epoch TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(condition_id)
);
