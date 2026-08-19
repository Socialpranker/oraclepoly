-- Migration 107: Track which markets each user has unlocked

CREATE TABLE IF NOT EXISTS user_market_unlocks (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    market_id TEXT NOT NULL,
    unlocked_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, market_id)
);

CREATE INDEX IF NOT EXISTS idx_user_market_unlocks_user ON user_market_unlocks(user_id);

-- RPC for claiming daily mission reward atomically
CREATE OR REPLACE FUNCTION claim_daily_mission_reward(
    p_user_id UUID,
    p_xp INT DEFAULT 500,
    p_credits INT DEFAULT 1
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    UPDATE users
    SET total_xp = COALESCE(total_xp, 0) + p_xp,
        xp = COALESCE(xp, 0) + p_xp,
        credits = COALESCE(credits, 0) + p_credits
    WHERE id = p_user_id;
END;
$$;
