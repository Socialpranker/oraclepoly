-- 20260320165534_whale_quality_columns.sql
-- Whale Quality: Empirical Bayes Quality Score (EBQ) columns

ALTER TABLE whales ADD COLUMN IF NOT EXISTS ebq_score NUMERIC(5,4);
ALTER TABLE whales ADD COLUMN IF NOT EXISTS ebq_ci_low NUMERIC(5,4);
ALTER TABLE whales ADD COLUMN IF NOT EXISTS ebq_ci_high NUMERIC(5,4);
ALTER TABLE whales ADD COLUMN IF NOT EXISTS predictive_roi NUMERIC(8,4);
ALTER TABLE whales ADD COLUMN IF NOT EXISTS signal_strength NUMERIC(6,4);
ALTER TABLE whales ADD COLUMN IF NOT EXISTS category_ebq JSONB DEFAULT '{}';
ALTER TABLE whales ADD COLUMN IF NOT EXISTS ebq_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_whales_ebq_score ON whales(ebq_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_whales_signal_strength ON whales(signal_strength DESC NULLS LAST);
