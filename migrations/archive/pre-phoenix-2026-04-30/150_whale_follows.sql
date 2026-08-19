-- 150: Whale Follows — подписка на алерты кита (Pro/Premium)
CREATE TABLE IF NOT EXISTS whale_follows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    whale_id UUID NOT NULL REFERENCES whales(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, whale_id)
);

CREATE INDEX IF NOT EXISTS idx_whale_follows_whale_id ON whale_follows(whale_id);
CREATE INDEX IF NOT EXISTS idx_whale_follows_user_id ON whale_follows(user_id);

COMMENT ON TABLE whale_follows IS 'Подписки пользователей на уведомления о сделках китов (Pro/Premium)';
