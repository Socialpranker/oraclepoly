-- Migration 103: Atomic RPC functions for race-condition-free operations
-- Fixes: credit deduction race condition, vote limit bypass

-- =============================================================================
-- 1. Atomic credit deduction
-- Returns remaining credits on success, -1 if insufficient
-- =============================================================================
CREATE OR REPLACE FUNCTION deduct_credit(p_telegram_id BIGINT, p_amount INT DEFAULT 1)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_remaining INT;
BEGIN
    UPDATE users
    SET credits = credits - p_amount
    WHERE telegram_id = p_telegram_id
      AND credits >= p_amount
    RETURNING credits INTO v_remaining;

    IF NOT FOUND THEN
        RETURN -1;
    END IF;

    RETURN v_remaining;
END;
$$;

-- =============================================================================
-- 2. Atomic vote casting with daily limit enforcement
-- Returns JSON with vote_id and votes_remaining_today
-- Raises exceptions for limit/duplicate violations
-- =============================================================================
CREATE OR REPLACE FUNCTION cast_vote_atomic(
    p_telegram_id BIGINT,
    p_market_id UUID,
    p_prediction TEXT,
    p_max_votes_per_day INT DEFAULT 3,
    p_xp_per_vote INT DEFAULT 10
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
BEGIN
    -- Get user ID
    SELECT id INTO v_user_id
    FROM users
    WHERE telegram_id = p_telegram_id;

    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'USER_NOT_FOUND';
    END IF;

    -- Check for existing vote on this market (within transaction lock)
    SELECT id INTO v_existing_vote
    FROM consensus_votes
    WHERE user_id = v_user_id AND market_id = p_market_id
    LIMIT 1;

    IF v_existing_vote IS NOT NULL THEN
        RAISE EXCEPTION 'ALREADY_VOTED';
    END IF;

    -- Count today's votes (locked by the transaction)
    SELECT COUNT(*) INTO v_votes_today
    FROM consensus_votes
    WHERE user_id = v_user_id
      AND created_at >= CURRENT_DATE;

    IF v_votes_today >= p_max_votes_per_day THEN
        RAISE EXCEPTION 'DAILY_LIMIT_REACHED';
    END IF;

    -- Insert vote
    INSERT INTO consensus_votes (user_id, market_id, prediction, rewarded, xp_earned)
    VALUES (v_user_id, p_market_id, p_prediction, FALSE, p_xp_per_vote)
    RETURNING id INTO v_vote_id;

    -- Award XP atomically
    UPDATE users
    SET total_xp = COALESCE(total_xp, 0) + p_xp_per_vote,
        xp = COALESCE(xp, 0) + p_xp_per_vote
    WHERE id = v_user_id;

    RETURN jsonb_build_object(
        'vote_id', v_vote_id,
        'votes_remaining_today', p_max_votes_per_day - v_votes_today - 1
    );
END;
$$;
