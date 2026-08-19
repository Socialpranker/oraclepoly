-- 162_paper_positions.sql
-- Paper Portfolio: виртуальные позиции по live signals для daily P&L tracking

CREATE TABLE IF NOT EXISTS paper_positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id UUID NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    event_id UUID,
    side TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    oracle_score_at_entry DOUBLE PRECISION NOT NULL,
    divergence_at_entry DOUBLE PRECISION NOT NULL,
    stake DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    category TEXT,
    question TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    pnl DOUBLE PRECISION,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_positions_open_market
    ON paper_positions (market_id) WHERE status = 'open';

CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_positions_open_event
    ON paper_positions (event_id) WHERE status = 'open' AND event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions (status);
CREATE INDEX IF NOT EXISTS idx_paper_positions_closed_at ON paper_positions (closed_at);
