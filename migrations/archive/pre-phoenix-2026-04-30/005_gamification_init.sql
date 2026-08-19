-- Migration: Gamification System Init
-- Date: 2026-02-12
-- Description: Adds gamification support (streaks, consensus votes, leaderboard)

-- ============================================
-- 1. EXTEND USERS TABLE
-- ============================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_streak INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_activity_date DATE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS total_xp INTEGER DEFAULT 0;

-- ============================================
-- 2. CREATE CONSENSUS_VOTES TABLE
-- ============================================

CREATE TABLE IF NOT EXISTS consensus_votes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    market_id TEXT NOT NULL,
    prediction TEXT NOT NULL CHECK (prediction IN ('YES', 'NO')),
    rewarded BOOLEAN DEFAULT FALSE,
    xp_earned INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(user_id, market_id)
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_consensus_votes_user_id ON consensus_votes(user_id);
CREATE INDEX IF NOT EXISTS idx_consensus_votes_market_id ON consensus_votes(market_id);
CREATE INDEX IF NOT EXISTS idx_consensus_votes_created_at ON consensus_votes(created_at);
CREATE INDEX IF NOT EXISTS idx_consensus_votes_user_date ON consensus_votes(user_id, created_at);

-- Enable RLS
ALTER TABLE consensus_votes ENABLE ROW LEVEL SECURITY;

-- Policies
CREATE POLICY "Users can read own votes" ON consensus_votes
    FOR SELECT USING (
        user_id IN (SELECT id FROM users WHERE telegram_id::text = auth.uid()::text)
        OR auth.role() = 'service_role'
    );

CREATE POLICY "Service role full access on votes" ON consensus_votes
    FOR ALL USING (auth.role() = 'service_role');

-- ============================================
-- 3. CREATE MATERIALIZED VIEW FOR LEADERBOARD
-- ============================================

CREATE MATERIALIZED VIEW IF NOT EXISTS top_analysts AS
SELECT
    u.id,
    u.telegram_id,
    u.username,
    u.display_name,
    u.avatar_url,
    u.total_xp,
    u.daily_streak,
    u.level,
    COUNT(cv.id) AS total_votes,
    COUNT(cv.id) FILTER (WHERE cv.rewarded = TRUE) AS correct_votes,
    CASE
        WHEN COUNT(cv.id) > 0
        THEN ROUND((COUNT(cv.id) FILTER (WHERE cv.rewarded = TRUE)::DECIMAL / COUNT(cv.id)) * 100, 2)
        ELSE 0
    END AS accuracy_rate,
    RANK() OVER (ORDER BY u.total_xp DESC) AS global_rank
FROM users u
LEFT JOIN consensus_votes cv ON u.id = cv.user_id
GROUP BY u.id, u.telegram_id, u.username, u.display_name, u.avatar_url, u.total_xp, u.daily_streak, u.level
ORDER BY u.total_xp DESC;

-- Index for efficient lookups
CREATE UNIQUE INDEX IF NOT EXISTS idx_top_analysts_id ON top_analysts(id);
CREATE INDEX IF NOT EXISTS idx_top_analysts_rank ON top_analysts(global_rank);

-- ============================================
-- 4. HELPER FUNCTION: REFRESH LEADERBOARD
-- ============================================

CREATE OR REPLACE FUNCTION refresh_top_analysts()
RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY top_analysts;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ============================================
-- 5. HELPER FUNCTION: COUNT TODAY'S VOTES
-- ============================================

CREATE OR REPLACE FUNCTION get_user_votes_today(p_user_id UUID)
RETURNS INTEGER AS $$
DECLARE
    vote_count INTEGER;
BEGIN
    SELECT COUNT(*)
    INTO vote_count
    FROM consensus_votes
    WHERE user_id = p_user_id
      AND created_at >= CURRENT_DATE
      AND created_at < CURRENT_DATE + INTERVAL '1 day';

    RETURN vote_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
