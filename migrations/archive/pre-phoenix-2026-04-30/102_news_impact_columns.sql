-- Add AI impact analysis columns to news_market_mappings
ALTER TABLE news_market_mappings
  ADD COLUMN IF NOT EXISTS impact_direction TEXT CHECK (impact_direction IN ('YES', 'NO', 'NEUTRAL')),
  ADD COLUMN IF NOT EXISTS impact_magnitude NUMERIC(5,4),
  ADD COLUMN IF NOT EXISTS probability_shift NUMERIC(5,4),
  ADD COLUMN IF NOT EXISTS impact_confidence NUMERIC(5,4),
  ADD COLUMN IF NOT EXISTS impact_reasoning TEXT,
  ADD COLUMN IF NOT EXISTS key_factors TEXT[],
  ADD COLUMN IF NOT EXISTS analyzed_at TIMESTAMPTZ;

-- Index for querying analyzed mappings
CREATE INDEX IF NOT EXISTS idx_news_market_mappings_analyzed
  ON news_market_mappings(market_id, analyzed_at DESC)
  WHERE analyzed_at IS NOT NULL;
