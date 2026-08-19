-- Migration: 122_create_market_analyses_table
-- Stores pre-generated Oracle Analyst output (computed analytics + LLM narratives)

CREATE TABLE market_analyses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id UUID NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    oracle_score_snapshot NUMERIC(5, 4),
    price_snapshot NUMERIC(10, 6),
    computed_analytics JSONB NOT NULL,
    scenarios JSONB,
    report JSONB,
    model_used TEXT DEFAULT 'gpt-4o-mini',
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_market_analyses_latest
    ON market_analyses(market_id, generated_at DESC);
