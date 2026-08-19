-- Migration 126: Add tab_type to user_market_unlocks for per-tab unlocking
-- Default 'all' preserves existing full unlocks

ALTER TABLE user_market_unlocks
ADD COLUMN IF NOT EXISTS tab_type TEXT NOT NULL DEFAULT 'all';

-- Drop old PK and create new one with tab_type
ALTER TABLE user_market_unlocks DROP CONSTRAINT IF EXISTS user_market_unlocks_pkey;
ALTER TABLE user_market_unlocks ADD PRIMARY KEY (user_id, market_id, tab_type);

-- Update deduct_credit RPC to accept variable amount
CREATE OR REPLACE FUNCTION deduct_credit(p_telegram_id BIGINT, p_amount INT DEFAULT 10)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    remaining INT;
BEGIN
    UPDATE users
    SET credits = credits - p_amount
    WHERE telegram_id = p_telegram_id AND credits >= p_amount
    RETURNING credits INTO remaining;

    IF NOT FOUND THEN
        RETURN -1;
    END IF;

    RETURN remaining;
END;
$$;
