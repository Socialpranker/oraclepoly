-- Add clob_token_ids and token_outcomes to markets table
ALTER TABLE markets ADD COLUMN IF NOT EXISTS clob_token_ids JSONB DEFAULT '[]'::jsonb;
ALTER TABLE markets ADD COLUMN IF NOT EXISTS token_outcomes JSONB DEFAULT '{}'::jsonb;
