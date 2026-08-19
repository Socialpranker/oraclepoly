-- migrations/172_goldsky_wallet_pnl.sql
CREATE TABLE IF NOT EXISTS wallet_pnl (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_address TEXT NOT NULL,
    total_pnl_usd NUMERIC(18, 2) DEFAULT 0,
    realized_pnl_usd NUMERIC(18, 2) DEFAULT 0,
    unrealized_pnl_usd NUMERIC(18, 2) DEFAULT 0,
    total_positions INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'goldsky',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(wallet_address)
);

-- UNIQUE(wallet_address) уже создаёт B-tree индекс, дополнительный не нужен
CREATE INDEX IF NOT EXISTS idx_wallet_pnl_pnl ON wallet_pnl(total_pnl_usd DESC);
