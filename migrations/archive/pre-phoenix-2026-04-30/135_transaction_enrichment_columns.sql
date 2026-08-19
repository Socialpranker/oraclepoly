-- Add enrichment columns to transactions table
-- for whale trade context (market info + trade price)
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS price NUMERIC(10, 6);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS market_title TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS market_slug TEXT;

-- outcome column already exists — no change needed, just stop hardcoding "YES"
