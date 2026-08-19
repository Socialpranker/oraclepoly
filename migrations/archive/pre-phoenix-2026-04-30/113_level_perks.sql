-- Migration 113: Level Perks System
-- Configurable perks per level: max votes, bonus credits on level-up, titles

CREATE TABLE IF NOT EXISTS level_perks (
    level INTEGER PRIMARY KEY,
    max_votes_per_day INTEGER NOT NULL DEFAULT 3,
    bonus_credits INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    has_profile_frame BOOLEAN DEFAULT FALSE
);

-- Seed level perks from roadmap
INSERT INTO level_perks (level, max_votes_per_day, bonus_credits, title, has_profile_frame) VALUES
    (1,  3, 0, 'Rookie',          FALSE),
    (2,  3, 1, 'Rookie',          FALSE),
    (3,  3, 0, 'Analyst',         FALSE),
    (4,  3, 0, 'Analyst',         FALSE),
    (5,  4, 0, 'Analyst',         FALSE),
    (6,  4, 0, 'Analyst',         FALSE),
    (7,  4, 3, 'Analyst',         FALSE),
    (8,  4, 0, 'Senior Analyst',  FALSE),
    (9,  4, 0, 'Senior Analyst',  FALSE),
    (10, 5, 0, 'Senior Analyst',  TRUE),
    (15, 5, 5, 'Oracle',          TRUE),
    (20, 6, 0, 'Grand Oracle',    TRUE)
ON CONFLICT (level) DO NOTHING;

-- ============================================
-- Function: get perks for a given level
-- Falls back to highest defined level <= target
-- ============================================
CREATE OR REPLACE FUNCTION get_level_perks(p_level INTEGER)
RETURNS TABLE(max_votes_per_day INTEGER, bonus_credits INTEGER, title TEXT, has_profile_frame BOOLEAN)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN QUERY
    SELECT lp.max_votes_per_day, lp.bonus_credits, lp.title, lp.has_profile_frame
    FROM level_perks lp
    WHERE lp.level <= p_level
    ORDER BY lp.level DESC
    LIMIT 1;
END;
$$;

-- ============================================
-- Update cast_vote_atomic to use dynamic max votes
-- ============================================
CREATE OR REPLACE FUNCTION cast_vote_atomic(
    p_telegram_id BIGINT,
    p_market_id TEXT,
    p_prediction TEXT,
    p_max_votes_per_day INTEGER DEFAULT 3,
    p_xp_per_vote INTEGER DEFAULT 10,
    p_confidence INTEGER DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_user_id UUID;
    v_user_level INTEGER;
    v_user_xp INTEGER;
    v_user_total_xp INTEGER;
    v_user_streak INTEGER;
    v_votes_today INTEGER;
    v_vote_id UUID;
    v_new_xp INTEGER;
    v_new_total_xp INTEGER;
    v_new_level INTEGER;
    v_xp_for_next INTEGER;
    v_leveled_up BOOLEAN := FALSE;
    v_streak_bonus NUMERIC;
    v_actual_xp INTEGER;
    v_dynamic_max_votes INTEGER;
    v_level_bonus_credits INTEGER := 0;
BEGIN
    -- Get user data
    SELECT id, level, xp, total_xp, COALESCE(daily_streak, 0)
    INTO v_user_id, v_user_level, v_user_xp, v_user_total_xp, v_user_streak
    FROM users WHERE telegram_id = p_telegram_id;

    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'USER_NOT_FOUND';
    END IF;

    -- Get dynamic max votes from level_perks (fallback to p_max_votes_per_day)
    SELECT COALESCE(lp.max_votes_per_day, p_max_votes_per_day)
    INTO v_dynamic_max_votes
    FROM level_perks lp
    WHERE lp.level <= v_user_level
    ORDER BY lp.level DESC
    LIMIT 1;

    IF v_dynamic_max_votes IS NULL THEN
        v_dynamic_max_votes := p_max_votes_per_day;
    END IF;

    -- Count today's votes
    SELECT COUNT(*) INTO v_votes_today
    FROM consensus_votes
    WHERE user_id = v_user_id AND created_at::date = CURRENT_DATE;

    IF v_votes_today >= v_dynamic_max_votes THEN
        RAISE EXCEPTION 'DAILY_LIMIT_REACHED';
    END IF;

    -- Check for duplicate vote
    IF EXISTS (SELECT 1 FROM consensus_votes WHERE user_id = v_user_id AND market_id = p_market_id) THEN
        RAISE EXCEPTION 'ALREADY_VOTED';
    END IF;

    -- Calculate XP with streak multiplier
    v_streak_bonus := LEAST(v_user_streak, 10) * 0.1;
    v_actual_xp := ROUND(p_xp_per_vote * (1 + v_streak_bonus));

    -- Insert vote
    INSERT INTO consensus_votes (user_id, market_id, prediction, confidence, xp_earned)
    VALUES (v_user_id, p_market_id, p_prediction, p_confidence, v_actual_xp)
    RETURNING id INTO v_vote_id;

    -- Update XP
    v_new_xp := COALESCE(v_user_xp, 0) + v_actual_xp;
    v_new_total_xp := COALESCE(v_user_total_xp, 0) + v_actual_xp;
    v_new_level := v_user_level;

    -- Check for level up using curve
    v_xp_for_next := get_xp_for_next_level(v_new_level);
    WHILE v_new_xp >= v_xp_for_next LOOP
        v_new_xp := v_new_xp - v_xp_for_next;
        v_new_level := v_new_level + 1;
        v_leveled_up := TRUE;
        v_xp_for_next := get_xp_for_next_level(v_new_level);

        -- Award bonus credits for reaching this level
        SELECT COALESCE(lp.bonus_credits, 0) INTO v_level_bonus_credits
        FROM level_perks lp WHERE lp.level = v_new_level;
    END LOOP;

    -- Update user
    IF v_leveled_up AND v_level_bonus_credits > 0 THEN
        UPDATE users SET xp = v_new_xp, total_xp = v_new_total_xp, level = v_new_level,
                         credits = credits + v_level_bonus_credits
        WHERE id = v_user_id;
    ELSE
        UPDATE users SET xp = v_new_xp, total_xp = v_new_total_xp, level = v_new_level
        WHERE id = v_user_id;
    END IF;

    RETURN jsonb_build_object(
        'vote_id', v_vote_id,
        'xp_earned', v_actual_xp,
        'streak_bonus', v_streak_bonus,
        'new_level', v_new_level,
        'leveled_up', v_leveled_up,
        'votes_remaining_today', v_dynamic_max_votes - v_votes_today - 1,
        'bonus_credits', v_level_bonus_credits
    );
END;
$$;
