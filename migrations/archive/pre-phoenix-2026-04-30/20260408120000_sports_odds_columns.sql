-- 20260408120000_sports_odds_columns.sql
-- Добавить bookmaker_probability в markets и sports_odds_raw в oracle_snapshots.

-- 1. Букмекерская вероятность в markets
ALTER TABLE markets ADD COLUMN IF NOT EXISTS bookmaker_probability NUMERIC(5, 4);
ALTER TABLE markets ADD COLUMN IF NOT EXISTS bookmaker_updated_at TIMESTAMPTZ;

-- 2. Sports odds raw factor в oracle_snapshots (для backtesting)
ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS sports_odds_raw NUMERIC(7, 4) DEFAULT 0;
