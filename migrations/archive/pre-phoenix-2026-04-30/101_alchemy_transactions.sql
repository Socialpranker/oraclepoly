-- ============================================================
-- ALCHEMY TRANSACTIONS SCHEMA UPDATE
-- 
-- Fixes unique constraint on transactions to allow multiple events per tx_hash
-- Adds missing columns for detailed tracking
-- ============================================================

-- 1. Drop existing unique constraint (tx_hash)
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_tx_hash_key;

-- 2. Add new columns
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS log_index INTEGER DEFAULT 0;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS from_address TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS to_address TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS asset TEXT;

-- 3. Add new composite unique constraint
-- We use a unique index which acts as a constraint but allows for concurrent creation if needed,
-- though standard constraint is fine here.
ALTER TABLE transactions ADD CONSTRAINT transactions_tx_hash_log_index_key UNIQUE (tx_hash, log_index);

-- 4. Update indexes
CREATE INDEX IF NOT EXISTS idx_transactions_from_address ON transactions(from_address);
CREATE INDEX IF NOT EXISTS idx_transactions_to_address ON transactions(to_address);
CREATE INDEX IF NOT EXISTS idx_transactions_asset ON transactions(asset);
