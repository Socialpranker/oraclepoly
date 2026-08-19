-- retail_trades: on-chain трейды из Goldsky Subgraph (все, не только киты)
-- Источник: Polymarket CTF Exchange OrderFilled events через The Graph / Goldsky
-- Назначение: Lambda_retail Bayes factor + улучшенный VPIN

CREATE TABLE IF NOT EXISTS retail_trades (
    id BIGSERIAL PRIMARY KEY,
    trade_id TEXT UNIQUE NOT NULL,        -- on-chain event ID (deduplicate)
    condition_id TEXT NOT NULL,            -- CTF condition_id → markets.condition_id
    maker TEXT NOT NULL,                   -- maker address
    taker TEXT NOT NULL,                   -- taker address
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),  -- taker side
    outcome TEXT NOT NULL CHECK (outcome IN ('YES', 'NO')),  -- token outcome
    price NUMERIC(10, 6) NOT NULL,        -- execution price
    amount NUMERIC(20, 6) NOT NULL,       -- USDC amount
    token_amount NUMERIC(20, 6),          -- outcome token amount
    block_number BIGINT NOT NULL,
    block_timestamp TIMESTAMPTZ NOT NULL,
    is_retail BOOLEAN DEFAULT true,        -- false если maker/taker в whales или smart_money_wallets
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Основные индексы
CREATE INDEX IF NOT EXISTS idx_retail_trades_condition_ts
    ON retail_trades (condition_id, block_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_retail_trades_block
    ON retail_trades (block_number DESC);
CREATE INDEX IF NOT EXISTS idx_retail_trades_timestamp
    ON retail_trades (block_timestamp DESC);

-- Для Lambda_retail: быстрый COUNT по condition + is_retail + side за последние N часов
CREATE INDEX IF NOT EXISTS idx_retail_trades_retail_signal
    ON retail_trades (condition_id, is_retail, side, block_timestamp DESC);

-- Партиционирование не нужно на текущих объёмах (<1M rows/month).
-- При росте >10M rows — мигрировать на monthly range partitions.

-- w_retail колонка в calibration_configs
ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS w_retail NUMERIC(6,4) DEFAULT 0.0;
