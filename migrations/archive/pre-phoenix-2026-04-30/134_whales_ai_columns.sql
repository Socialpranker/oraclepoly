-- ============================================
-- ADD MISSING AI-ANALYSIS COLUMNS TO WHALES
-- whale_watcher.py writes these from whale_analyst output,
-- but they were never added to the schema.
-- ============================================

ALTER TABLE whales
    ADD COLUMN IF NOT EXISTS trading_pattern TEXT DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS wallet_classification TEXT DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS anomaly_reason TEXT;
