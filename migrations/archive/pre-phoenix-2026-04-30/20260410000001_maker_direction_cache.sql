-- migrations/20260410000001_maker_direction_cache.sql
-- Live snapshot maker direction из CLOB /book. TTL 2h, обновляется clob-book-sync каждый час.

CREATE TABLE IF NOT EXISTS maker_direction_cache (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id             UUID NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    condition_id          TEXT NOT NULL,
    maker_direction_ratio NUMERIC(6,4) NOT NULL,  -- [-1.0, 1.0]: >0 = makers bet YES, <0 = NO
    depth_imbalance       NUMERIC(6,4),            -- (bid_depth - ask_depth) / total, nullable
    bid_depth_usd         NUMERIC(16,2),
    ask_depth_usd         NUMERIC(16,2),
    maker_count           INTEGER DEFAULT 0,
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT maker_direction_ratio_range CHECK (maker_direction_ratio BETWEEN -1.0 AND 1.0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_maker_direction_cache_market
    ON maker_direction_cache (market_id);

CREATE INDEX IF NOT EXISTS idx_maker_direction_cache_fetched
    ON maker_direction_cache (fetched_at);

COMMENT ON TABLE maker_direction_cache IS
    'Live snapshot maker direction из CLOB /book. TTL 2h, обновляется clob-book-sync каждый час.';
COMMENT ON COLUMN maker_direction_cache.maker_direction_ratio IS
    'Нормализованный вектор: (maker_yes_vol - maker_no_vol) / total_maker_vol. +1.0 = все makers ставят YES.';
