-- Migration 132: Vote snapshots — store market context at vote time
-- Supports: prediction history, outcome tracking, user notes

CREATE TABLE vote_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vote_id UUID NOT NULL UNIQUE REFERENCES consensus_votes(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    market_id TEXT NOT NULL,

    -- Market state at vote time
    oracle_score NUMERIC(5, 4),
    current_price NUMERIC(10, 6),
    sentiment_score NUMERIC(5, 4),
    whale_confidence NUMERIC(5, 4),
    risk_score NUMERIC(5, 4),
    divergence_signal TEXT,
    ai_hint TEXT,

    -- Unlocked insights snapshot (only paid tabs)
    unlocked_tabs TEXT[] DEFAULT '{}',
    tab_insights JSONB DEFAULT '{}',

    -- User annotations (added later in history view)
    user_notes TEXT,

    -- Outcome tracking (updated when market resolves)
    outcome TEXT DEFAULT 'pending' CHECK (outcome IN ('pending', 'win', 'loss', 'void')),
    price_at_resolve NUMERIC(10, 6),
    resolved_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_vote_snapshots_user ON vote_snapshots(user_id, created_at DESC);
CREATE INDEX idx_vote_snapshots_market ON vote_snapshots(market_id);
CREATE INDEX idx_vote_snapshots_outcome ON vote_snapshots(outcome) WHERE outcome = 'pending';

-- RLS
ALTER TABLE vote_snapshots ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own snapshots" ON vote_snapshots
    FOR SELECT USING (
        user_id IN (SELECT id FROM users WHERE telegram_id::text = auth.uid()::text)
        OR auth.role() = 'service_role'
    );

CREATE POLICY "Service role full access on snapshots" ON vote_snapshots
    FOR ALL USING (auth.role() = 'service_role');
