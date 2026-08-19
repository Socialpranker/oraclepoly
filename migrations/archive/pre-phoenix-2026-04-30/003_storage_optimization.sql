-- Remove redundant index from transactions table
-- tx_hash is already UNIQUE, which creates a B-tree index automatically.
DROP INDEX IF EXISTS idx_transactions_tx_hash;

-- Optimization: Add index for cleanup queries if not already present
-- We will be deleting by created_at and amount/is_whale
CREATE INDEX IF NOT EXISTS idx_transactions_is_whale ON transactions(is_whale);
CREATE INDEX IF NOT EXISTS idx_news_scraped_at ON news(scraped_at);
