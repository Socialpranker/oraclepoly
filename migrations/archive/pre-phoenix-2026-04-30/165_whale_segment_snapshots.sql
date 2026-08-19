CREATE TABLE IF NOT EXISTS whale_segment_snapshots (
    id SERIAL PRIMARY KEY,
    snapshot_date DATE NOT NULL,
    segment TEXT NOT NULL,
    initial_bank NUMERIC(12,2) NOT NULL DEFAULT 0,
    final_bank NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_pnl NUMERIC(12,2) NOT NULL DEFAULT 0,
    roi_percent NUMERIC(8,2) NOT NULL DEFAULT 0,
    total_bets INT NOT NULL DEFAULT 0,
    wins INT NOT NULL DEFAULT 0,
    losses INT NOT NULL DEFAULT 0,
    win_rate NUMERIC(6,2) NOT NULL DEFAULT 0,
    max_drawdown NUMERIC(12,2) NOT NULL DEFAULT 0,
    sharpe_ratio NUMERIC(8,4) NOT NULL DEFAULT 0,
    profit_factor NUMERIC(8,4) NOT NULL DEFAULT 0,
    classified_txs INT NOT NULL DEFAULT 0,
    params JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(snapshot_date, segment)
);

CREATE INDEX IF NOT EXISTS idx_whale_segment_snapshots_date ON whale_segment_snapshots(snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_whale_segment_snapshots_segment ON whale_segment_snapshots(segment);
