-- 176_live_paper_positions.sql
-- Live Trading Paper Portfolio: автоматические позиции от 12 моделей (snapshot daily)

CREATE TABLE IF NOT EXISTS live_paper_positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model TEXT NOT NULL,
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
    snapshot_date DATE NOT NULL DEFAULT CURRENT_DATE,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ
);

-- Одна open позиция на модель × рынок × дату
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_paper_open
    ON live_paper_positions (model, market_id, snapshot_date)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_live_paper_status ON live_paper_positions (status);
CREATE INDEX IF NOT EXISTS idx_live_paper_model ON live_paper_positions (model);
CREATE INDEX IF NOT EXISTS idx_live_paper_snapshot_date ON live_paper_positions (snapshot_date);
CREATE INDEX IF NOT EXISTS idx_live_paper_closed_at ON live_paper_positions (closed_at);
