-- Migration 002: Add horizon column to markets table
-- Required for Fast/Mid/Long track filtering in UI

-- Add horizon column
ALTER TABLE markets
ADD COLUMN IF NOT EXISTS horizon TEXT DEFAULT 'mid'
CHECK (horizon IN ('fast', 'mid', 'long'));

-- Add index for filtering
CREATE INDEX IF NOT EXISTS idx_markets_horizon ON markets(horizon);

-- Add is_whale column to transactions (for whale highlighting)
ALTER TABLE transactions
ADD COLUMN IF NOT EXISTS is_whale BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_transactions_is_whale ON transactions(is_whale);

-- Comment for documentation
COMMENT ON COLUMN markets.horizon IS 'Market time horizon: fast (<7 days), mid (7-30 days), long (>30 days)';
