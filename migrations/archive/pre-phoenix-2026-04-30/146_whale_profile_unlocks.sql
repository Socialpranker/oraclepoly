-- Migration 145: Whale profile unlocks
-- Добавляем поддержку разблокировки whale-профилей через Sparks

ALTER TABLE user_market_unlocks
ADD COLUMN IF NOT EXISTS whale_id UUID REFERENCES whales(id);

CREATE INDEX IF NOT EXISTS idx_user_market_unlocks_whale
ON user_market_unlocks(user_id, whale_id)
WHERE whale_id IS NOT NULL;
