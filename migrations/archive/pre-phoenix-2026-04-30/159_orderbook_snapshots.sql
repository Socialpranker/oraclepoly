-- migrations/159_orderbook_snapshots.sql
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mid_price NUMERIC(10,6),
    spread NUMERIC(10,6),
    bids JSONB,
    asks JSONB,
    total_bid_depth NUMERIC(12,2),
    total_ask_depth NUMERIC(12,2)
);

CREATE INDEX IF NOT EXISTS idx_ob_snap_market_time
    ON orderbook_snapshots(market_id, captured_at DESC);
