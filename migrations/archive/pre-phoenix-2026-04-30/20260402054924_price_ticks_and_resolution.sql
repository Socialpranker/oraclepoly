-- price_ticks: 5-мин ценовые тики для exit backtest
CREATE TABLE IF NOT EXISTS price_ticks (
    id BIGSERIAL PRIMARY KEY,
    market_id UUID NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    token_id TEXT NOT NULL,
    price NUMERIC(7,4) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL DEFAULT 'clob'
);

CREATE INDEX IF NOT EXISTS idx_price_ticks_market_time ON price_ticks (market_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_price_ticks_time_brin ON price_ticks USING brin (timestamp) WITH (pages_per_range = 64);
CREATE UNIQUE INDEX IF NOT EXISTS idx_price_ticks_dedup ON price_ticks (market_id, token_id, timestamp);

-- Resolution columns в markets
ALTER TABLE markets ADD COLUMN IF NOT EXISTS resolved BOOLEAN DEFAULT false;
ALTER TABLE markets ADD COLUMN IF NOT EXISTS winner TEXT;
