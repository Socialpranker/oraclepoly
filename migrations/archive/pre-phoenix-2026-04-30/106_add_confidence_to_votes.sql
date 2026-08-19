-- Migration 106: Add confidence column to consensus_votes and update RPC

-- 1. Add confidence column (nullable for backwards compatibility)
ALTER TABLE consensus_votes ADD COLUMN IF NOT EXISTS confidence INTEGER CHECK (confidence >= 50 AND confidence <= 100);

-- 2. Update cast_vote_atomic to accept and store confidence
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
BEGIN
    -- Get user ID
    SELECT id INTO v_user_id
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

    -- Insert vote with confidence
    INSERT INTO consensus_votes (user_id, market_id, prediction, rewarded, xp_earned, confidence)
    VALUES (v_user_id, p_market_id, p_prediction, FALSE, p_xp_per_vote, p_confidence)
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
