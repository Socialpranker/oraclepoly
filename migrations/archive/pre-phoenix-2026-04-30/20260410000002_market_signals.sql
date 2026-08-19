-- migrations/20260410000002_market_signals.sql
-- Агрегированные торговые сигналы per market.
-- Backfill из /mnt/data/polymarket_dataset, обновляется live.

CREATE TABLE IF NOT EXISTS market_signals (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id              UUID NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    condition_id           TEXT NOT NULL,
    taker_buy_volume_usd   NUMERIC(20,6) DEFAULT 0,
    taker_sell_volume_usd  NUMERIC(20,6) DEFAULT 0,
    taker_imbalance        NUMERIC(6,4),   -- (buy - sell) / total, [-1, 1]
    total_trade_count      INTEGER DEFAULT 0,
    whale_trade_count      INTEGER DEFAULT 0,
    avg_trade_size_usd     NUMERIC(16,2),
    data_source            TEXT NOT NULL DEFAULT 'historical',  -- 'historical' | 'live'
    period_start           TIMESTAMPTZ,
    period_end             TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT taker_imbalance_range CHECK (taker_imbalance BETWEEN -1.0 AND 1.0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_market_signals_market
    ON market_signals (market_id);

CREATE INDEX IF NOT EXISTS idx_market_signals_updated
    ON market_signals (updated_at);

COMMENT ON TABLE market_signals IS
    'Агрегированные торговые сигналы per market. Backfill из /mnt/data, обновляется live.';
