-- Migration 108: Streak multiplier in cast_vote_atomic + level progression curve
--
-- Changes:
-- 1. cast_vote_atomic now reads daily_streak and applies XP multiplier
--    Formula: xp = base_xp * (1 + min(streak, 10) * 0.1)
-- 2. New function get_xp_for_next_level(level) for non-linear progression
-- 3. cast_vote_atomic now handles level-up with the new curve

-- Helper function: XP required to go from level N to level N+1
CREATE OR REPLACE FUNCTION get_xp_for_next_level(p_level INT)
RETURNS INT
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN CASE
        WHEN p_level = 1 THEN 200
        WHEN p_level = 2 THEN 400
        WHEN p_level = 3 THEN 600
        WHEN p_level = 4 THEN 800
        WHEN p_level = 5 THEN 1000
        WHEN p_level = 6 THEN 1200
        WHEN p_level = 7 THEN 1400
        WHEN p_level = 8 THEN 1600
        WHEN p_level = 9 THEN 2000
        ELSE 2500  -- level 10+
    END;
END;
$$;

-- Helper function: cumulative XP required to reach level N (from level 1)
CREATE OR REPLACE FUNCTION get_cumulative_xp_for_level(p_level INT)
RETURNS INT
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    v_total INT := 0;
    v_i INT;
BEGIN
    FOR v_i IN 1..(p_level - 1) LOOP
        v_total := v_total + get_xp_for_next_level(v_i);
    END LOOP;
    RETURN v_total;
END;
$$;

-- Updated cast_vote_atomic with streak multiplier and level-up logic
CREATE OR REPLACE FUNCTION cast_vote_atomic(
    p_telegram_id BIGINT,
    p_market_id UUID,
    p_prediction TEXT,
    p_max_votes_per_day INT DEFAULT 3,
    p_xp_per_vote INT DEFAULT 10,
    p_confidence INT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_user_id UUID;
    v_votes_today INT;
    v_vote_id UUID;
    v_existing_vote UUID;
    v_streak INT;
    v_actual_xp INT;
    v_new_total_xp INT;
    v_new_xp INT;
    v_current_level INT;
    v_new_level INT;
    v_xp_needed INT;
BEGIN
    -- Get user ID and streak
    SELECT id, COALESCE(daily_streak, 0), COALESCE(total_xp, 0), COALESCE(xp, 0), COALESCE(level, 1)
    INTO v_user_id, v_streak, v_new_total_xp, v_new_xp, v_current_level
    FROM users
    WHERE telegram_id = p_telegram_id;

    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'USER_NOT_FOUND';
    END IF;

    -- Check for existing vote on this market
    SELECT id INTO v_existing_vote
    FROM consensus_votes
    WHERE user_id = v_user_id AND market_id = p_market_id
    LIMIT 1;

    IF v_existing_vote IS NOT NULL THEN
        RAISE EXCEPTION 'ALREADY_VOTED';
    END IF;

    -- Count today's votes
    SELECT COUNT(*) INTO v_votes_today
    FROM consensus_votes
    WHERE user_id = v_user_id
      AND created_at >= CURRENT_DATE;

    IF v_votes_today >= p_max_votes_per_day THEN
        RAISE EXCEPTION 'DAILY_LIMIT_REACHED';
    END IF;

    -- Apply streak multiplier: xp = base_xp * (1 + min(streak, 10) * 0.1)
    v_actual_xp := ROUND(p_xp_per_vote * (1.0 + LEAST(v_streak, 10) * 0.1));

    -- Insert vote with actual XP (including streak bonus)
    INSERT INTO consensus_votes (user_id, market_id, prediction, rewarded, xp_earned, confidence)
    VALUES (v_user_id, p_market_id, p_prediction, FALSE, v_actual_xp, p_confidence)
    RETURNING id INTO v_vote_id;

    -- Update user XP
    v_new_total_xp := v_new_total_xp + v_actual_xp;
    v_new_xp := v_new_xp + v_actual_xp;

    -- Check for level-up(s) using the progression curve
    v_new_level := v_current_level;
    LOOP
        v_xp_needed := get_cumulative_xp_for_level(v_new_level + 1);
        EXIT WHEN v_new_total_xp < v_xp_needed;
        v_new_level := v_new_level + 1;
    END LOOP;

    -- Update user
    UPDATE users
    SET total_xp = v_new_total_xp,
        xp = v_new_xp,
        level = v_new_level
    WHERE id = v_user_id;

    RETURN jsonb_build_object(
        'vote_id', v_vote_id,
        'votes_remaining_today', p_max_votes_per_day - v_votes_today - 1,
        'xp_earned', v_actual_xp,
        'streak_bonus', v_actual_xp - p_xp_per_vote,
        'new_level', v_new_level,
        'leveled_up', v_new_level > v_current_level
    );
END;
$$;
