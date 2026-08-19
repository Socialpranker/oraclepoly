-- Добавить колонки метрик в whales
ALTER TABLE whales ADD COLUMN IF NOT EXISTS pnl_usd NUMERIC DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS roi_percent NUMERIC DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS wins INTEGER DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS losses INTEGER DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS total_invested NUMERIC DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS timing_score_avg REAL DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS is_arbitrageur BOOLEAN DEFAULT FALSE;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS arb_ratio REAL DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS metrics_updated_at TIMESTAMPTZ;

-- Добавить timing_score и is_arbitrage в transactions
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS timing_score REAL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS is_arbitrage BOOLEAN DEFAULT FALSE;

-- Индексы для аналитических запросов
CREATE INDEX IF NOT EXISTS idx_whales_wins_losses ON whales(wins, losses)
    WHERE wins > 0 OR losses > 0;
CREATE INDEX IF NOT EXISTS idx_whales_is_arbitrageur ON whales(is_arbitrageur)
    WHERE is_arbitrageur = TRUE;
CREATE INDEX IF NOT EXISTS idx_transactions_market_whale ON transactions(market_id, whale_id)
    WHERE is_whale = TRUE;
CREATE INDEX IF NOT EXISTS idx_transactions_not_arb ON transactions(market_id)
    WHERE is_arbitrage = FALSE AND is_whale = TRUE;
