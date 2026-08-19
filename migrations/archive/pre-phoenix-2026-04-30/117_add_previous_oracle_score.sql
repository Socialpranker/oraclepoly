-- Add previous_oracle_score column for tracking score changes (Top Movers)
ALTER TABLE markets ADD COLUMN IF NOT EXISTS previous_oracle_score NUMERIC(5, 4);
