-- Migration: strategy_validations table
-- Stores weekly Lab-based validation results per strategy (alert-only, no auto-kill).
-- Created: 2026-04-20

CREATE TABLE IF NOT EXISTS strategy_validations (
    id              BIGSERIAL PRIMARY KEY,
    strategy_key    TEXT NOT NULL REFERENCES strategy_state(strategy_key) ON DELETE CASCADE,
    validated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    source          TEXT NOT NULL,          -- 'all' | 'live' | 'hf'
    total_bets      INT NOT NULL,
    roi_pct         NUMERIC(8, 2) NOT NULL,
    sharpe          NUMERIC(6, 3),
    max_dd_pct      NUMERIC(6, 2),
    win_rate        NUMERIC(5, 2),
    ci_95_roi_low   NUMERIC(8, 2),
    ci_95_roi_high  NUMERIC(8, 2),
    components_used TEXT[],
    verdict         TEXT NOT NULL,          -- 'healthy' | 'marginal' | 'unhealthy' | 'insufficient_data'
    issues          TEXT[] DEFAULT '{}',
    raw_result      JSONB
);

CREATE INDEX IF NOT EXISTS idx_strategy_validations_key_time
    ON strategy_validations (strategy_key, validated_at DESC);
