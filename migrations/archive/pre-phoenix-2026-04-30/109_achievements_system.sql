-- Migration 109: Achievements system — tables, seed data, check function

-- 1. Achievements config table
CREATE TABLE IF NOT EXISTS achievements_config (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    icon TEXT NOT NULL,
    rarity TEXT NOT NULL CHECK (rarity IN ('common', 'rare', 'epic', 'legendary')),
    condition_type TEXT NOT NULL,
    condition_value INTEGER NOT NULL
);

-- 2. User achievements table
CREATE TABLE IF NOT EXISTS user_achievements (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    achievement_id TEXT NOT NULL REFERENCES achievements_config(id),
    unlocked_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, achievement_id)
);

CREATE INDEX IF NOT EXISTS idx_user_achievements_user ON user_achievements(user_id);

-- 3. Seed 19 achievements
INSERT INTO achievements_config (id, title, icon, rarity, condition_type, condition_value) VALUES
    -- Common (5)
    ('first_vote',    'First Call',      '🎯', 'common',    'total_votes',   1),
    ('first_correct', 'On The Money',    '💰', 'common',    'correct_votes', 1),
    ('first_unlock',  'Deep Diver',      '🔍', 'common',    'total_unlocks', 1),
    ('streak_3',      'Warming Up',      '🔥', 'common',    'daily_streak',  3),
    ('votes_10',      'Getting Started', '📊', 'common',    'total_votes',   10),
    -- Rare (5)
    ('streak_7',      'On Fire',         '🔥', 'rare',      'daily_streak',  7),
    ('votes_50',      'Market Watcher',  '👁️', 'rare',      'total_votes',   50),
    ('accuracy_60',   'Sharp Eye',       '🎯', 'rare',      'accuracy_pct',  60),
    ('level_5',       'Rising Star',     '⭐', 'rare',      'level',         5),
    ('referral_1',    'Networker',       '🤝', 'rare',      'referral_count',1),
    -- Epic (5)
    ('streak_30',     'Unstoppable',     '⚡', 'epic',      'daily_streak',  30),
    ('votes_200',     'Market Oracle',   '🏛️', 'epic',      'total_votes',   200),
    ('accuracy_75',   'Prophet',         '🔮', 'epic',      'accuracy_pct',  75),
    ('level_10',      'Senior Oracle',   '👑', 'epic',      'level',         10),
    ('referral_5',    'Community Builder','🏗️', 'epic',      'referral_count',5),
    -- Legendary (4)
    ('streak_100',    'Titan',           '💎', 'legendary', 'daily_streak',  100),
    ('accuracy_90',   'The Oracle',      '🌟', 'legendary', 'accuracy_pct',  90),
    ('top_3',         'Podium Legend',   '🏆', 'legendary', 'leaderboard_rank', 3),
    ('all_categories','Renaissance',     '🎨', 'legendary', 'mastery_all_3', 1)
ON CONFLICT (id) DO NOTHING;

-- 4. Function to check and award achievements for a user
CREATE OR REPLACE FUNCTION check_achievements(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_user RECORD;
    v_achievement RECORD;
    v_user_value INT;
    v_newly_unlocked TEXT[] := '{}';
    v_total_votes INT;
    v_correct_votes INT;
    v_total_unlocks INT;
    v_accuracy_pct INT;
BEGIN
    -- Get user stats
    SELECT level, daily_streak
    INTO v_user
    FROM users WHERE id = p_user_id;

    IF NOT FOUND THEN RETURN '{"unlocked": []}'::JSONB; END IF;

    -- Aggregate vote stats
    SELECT
        COUNT(*),
        COUNT(*) FILTER (WHERE rewarded = TRUE AND xp_earned > 0)
    INTO v_total_votes, v_correct_votes
    FROM consensus_votes WHERE user_id = p_user_id;

    -- Accuracy (only if min votes met)
    IF v_total_votes >= 10 THEN
        v_accuracy_pct := ROUND(v_correct_votes * 100.0 / NULLIF(v_total_votes, 0));
    ELSE
        v_accuracy_pct := 0;
    END IF;

    -- Total unlocks
    SELECT COUNT(*) INTO v_total_unlocks
    FROM user_market_unlocks WHERE user_id = p_user_id;

    -- Check each achievement
    FOR v_achievement IN
        SELECT * FROM achievements_config
        WHERE id NOT IN (
            SELECT achievement_id FROM user_achievements WHERE user_id = p_user_id
        )
    LOOP
        v_user_value := CASE v_achievement.condition_type
            WHEN 'total_votes'    THEN v_total_votes
            WHEN 'correct_votes'  THEN v_correct_votes
            WHEN 'total_unlocks'  THEN v_total_unlocks
            WHEN 'daily_streak'   THEN v_user.daily_streak
            WHEN 'level'          THEN v_user.level
            WHEN 'accuracy_pct'   THEN v_accuracy_pct
            -- referral_count, leaderboard_rank, mastery_all_3 checked by batch job
            ELSE -1
        END;

        IF v_user_value >= v_achievement.condition_value THEN
            INSERT INTO user_achievements (user_id, achievement_id)
            VALUES (p_user_id, v_achievement.id)
            ON CONFLICT DO NOTHING;
            v_newly_unlocked := array_append(v_newly_unlocked, v_achievement.id);
        END IF;
    END LOOP;

    RETURN jsonb_build_object('unlocked', to_jsonb(v_newly_unlocked));
END;
$$;
