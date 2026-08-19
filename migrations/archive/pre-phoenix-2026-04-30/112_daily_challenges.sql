-- Migration 112: Daily Challenges System
-- Full challenge generation, tracking, and reward system

-- Table to store daily challenges per user
CREATE TABLE IF NOT EXISTS daily_challenges (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    challenge_date DATE NOT NULL DEFAULT CURRENT_DATE,
    challenge_type TEXT NOT NULL,
    challenge_params JSONB DEFAULT '{}',
    description TEXT NOT NULL,
    xp_reward INTEGER NOT NULL DEFAULT 15,
    completed BOOLEAN DEFAULT FALSE,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_challenges_user_date_type
    ON daily_challenges(user_id, challenge_date, challenge_type);
CREATE INDEX IF NOT EXISTS idx_daily_challenges_lookup
    ON daily_challenges(user_id, challenge_date);

-- Track bonus claim (all 3 completed)
-- Using a separate table to avoid cache-based tracking
CREATE TABLE IF NOT EXISTS daily_challenge_claims (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    claim_date DATE NOT NULL DEFAULT CURRENT_DATE,
    bonus_xp INTEGER NOT NULL DEFAULT 30,
    claimed_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, claim_date)
);

-- ============================================
-- RPC: Generate daily challenges for a user
-- Called lazily on GET /me/challenges (if none exist for today)
-- ============================================
CREATE OR REPLACE FUNCTION generate_daily_challenges(p_user_id UUID)
RETURNS SETOF daily_challenges
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_today DATE := CURRENT_DATE;
    v_streak INTEGER;
    v_total_votes INTEGER;
    v_categories TEXT[];
    v_chosen_cat TEXT;
    v_challenge_types TEXT[] := ARRAY['vote_count', 'vote_category', 'unlock_insight', 'check_whales', 'streak_keep', 'accuracy_target'];
    v_selected TEXT[];
    v_type TEXT;
    v_desc TEXT;
    v_params JSONB;
BEGIN
    -- Check if challenges already exist for today
    IF EXISTS (SELECT 1 FROM daily_challenges WHERE user_id = p_user_id AND challenge_date = v_today) THEN
        RETURN QUERY SELECT * FROM daily_challenges WHERE user_id = p_user_id AND challenge_date = v_today ORDER BY created_at;
        RETURN;
    END IF;

    -- Get user context for smart challenge selection
    SELECT COALESCE(daily_streak, 0) INTO v_streak FROM users WHERE id = p_user_id;
    SELECT COUNT(*) INTO v_total_votes FROM consensus_votes WHERE user_id = p_user_id;

    -- Get categories user has voted in
    SELECT ARRAY_AGG(DISTINCT m.category)
    INTO v_categories
    FROM consensus_votes cv
    JOIN markets m ON m.id = cv.market_id
    WHERE cv.user_id = p_user_id AND m.category IS NOT NULL;

    IF v_categories IS NULL THEN
        v_categories := ARRAY['Crypto', 'Politics', 'Sports'];
    END IF;

    -- Pick a random category for vote_category challenge
    v_chosen_cat := v_categories[1 + floor(random() * array_length(v_categories, 1))::int];

    -- Select 3 challenge types (semi-random, context-aware)
    -- Always include vote_count as first challenge (most universal)
    v_selected := ARRAY['vote_count'];

    -- Second challenge: if streak > 0, include streak_keep; otherwise vote_category
    IF v_streak > 0 THEN
        v_selected := v_selected || 'streak_keep';
    ELSE
        v_selected := v_selected || 'vote_category';
    END IF;

    -- Third challenge: rotate between unlock_insight, check_whales, accuracy_target
    -- Use day-of-year modulo to rotate predictably
    CASE (EXTRACT(DOY FROM v_today)::int % 3)
        WHEN 0 THEN v_selected := v_selected || 'unlock_insight';
        WHEN 1 THEN v_selected := v_selected || 'check_whales';
        WHEN 2 THEN v_selected := v_selected || 'accuracy_target';
    END CASE;

    -- Generate each challenge
    FOREACH v_type IN ARRAY v_selected LOOP
        v_params := '{}';
        CASE v_type
            WHEN 'vote_count' THEN
                v_desc := 'Vote on 2 markets today';
                v_params := '{"target": 2}';
            WHEN 'vote_category' THEN
                v_desc := 'Vote on a ' || v_chosen_cat || ' market';
                v_params := jsonb_build_object('category', v_chosen_cat, 'target', 1);
            WHEN 'unlock_insight' THEN
                v_desc := 'Unlock a market insight';
                v_params := '{"target": 1}';
            WHEN 'check_whales' THEN
                v_desc := 'Check whale activity';
                v_params := '{"target": 1}';
            WHEN 'streak_keep' THEN
                v_desc := 'Keep your voting streak alive';
                v_params := '{"target": 1}';
            WHEN 'accuracy_target' THEN
                v_desc := 'Get a correct prediction';
                v_params := '{"target": 1}';
        END CASE;

        INSERT INTO daily_challenges (user_id, challenge_date, challenge_type, challenge_params, description, xp_reward)
        VALUES (p_user_id, v_today, v_type, v_params, v_desc, 15)
        ON CONFLICT (user_id, challenge_date, challenge_type) DO NOTHING;
    END LOOP;

    RETURN QUERY SELECT * FROM daily_challenges WHERE user_id = p_user_id AND challenge_date = v_today ORDER BY created_at;
END;
$$;

-- ============================================
-- RPC: Check and complete challenges for a user
-- Called after vote, unlock, or whale check
-- ============================================
CREATE OR REPLACE FUNCTION check_daily_challenges(p_user_id UUID, p_action TEXT, p_params JSONB DEFAULT '{}')
RETURNS TABLE(challenge_id UUID, challenge_type TEXT, just_completed BOOLEAN, xp_awarded INTEGER)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_today DATE := CURRENT_DATE;
    v_challenge RECORD;
    v_count INTEGER;
    v_target INTEGER;
    v_completed BOOLEAN;
BEGIN
    -- Ensure challenges exist for today
    PERFORM generate_daily_challenges(p_user_id);

    FOR v_challenge IN
        SELECT * FROM daily_challenges
        WHERE user_id = p_user_id AND challenge_date = v_today AND completed = FALSE
    LOOP
        v_completed := FALSE;
        v_target := COALESCE((v_challenge.challenge_params->>'target')::int, 1);

        CASE v_challenge.challenge_type
            WHEN 'vote_count' THEN
                IF p_action = 'vote' THEN
                    SELECT COUNT(*) INTO v_count
                    FROM consensus_votes
                    WHERE user_id = p_user_id AND created_at::date = v_today;
                    v_completed := v_count >= v_target;
                END IF;

            WHEN 'vote_category' THEN
                IF p_action = 'vote' THEN
                    SELECT COUNT(*) INTO v_count
                    FROM consensus_votes cv
                    JOIN markets m ON m.id = cv.market_id
                    WHERE cv.user_id = p_user_id
                      AND cv.created_at::date = v_today
                      AND m.category = v_challenge.challenge_params->>'category';
                    v_completed := v_count >= v_target;
                END IF;

            WHEN 'unlock_insight' THEN
                IF p_action = 'unlock' THEN
                    SELECT COUNT(*) INTO v_count
                    FROM user_market_unlocks
                    WHERE user_id = p_user_id AND unlocked_at::date = v_today;
                    v_completed := v_count >= v_target;
                END IF;

            WHEN 'check_whales' THEN
                IF p_action = 'check_whales' THEN
                    v_completed := TRUE;
                END IF;

            WHEN 'streak_keep' THEN
                IF p_action = 'vote' THEN
                    v_completed := TRUE; -- any vote keeps streak
                END IF;

            WHEN 'accuracy_target' THEN
                IF p_action = 'correct_prediction' THEN
                    v_completed := TRUE;
                END IF;
        END CASE;

        IF v_completed THEN
            UPDATE daily_challenges
            SET completed = TRUE, completed_at = NOW()
            WHERE id = v_challenge.id;

            -- Award XP
            UPDATE users
            SET xp = xp + v_challenge.xp_reward,
                total_xp = total_xp + v_challenge.xp_reward
            WHERE id = p_user_id;
        END IF;

        challenge_id := v_challenge.id;
        challenge_type := v_challenge.challenge_type;
        just_completed := v_completed;
        xp_awarded := CASE WHEN v_completed THEN v_challenge.xp_reward ELSE 0 END;
        RETURN NEXT;
    END LOOP;
END;
$$;

-- ============================================
-- RPC: Claim bonus for completing all 3 challenges
-- ============================================
CREATE OR REPLACE FUNCTION claim_daily_challenge_bonus(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_today DATE := CURRENT_DATE;
    v_total INTEGER;
    v_completed INTEGER;
    v_bonus_xp INTEGER := 30;
BEGIN
    -- Count total and completed challenges for today
    SELECT COUNT(*), COUNT(*) FILTER (WHERE completed = TRUE)
    INTO v_total, v_completed
    FROM daily_challenges
    WHERE user_id = p_user_id AND challenge_date = v_today;

    IF v_total = 0 THEN
        RETURN jsonb_build_object('success', false, 'error', 'NO_CHALLENGES');
    END IF;

    IF v_completed < v_total THEN
        RETURN jsonb_build_object('success', false, 'error', 'NOT_ALL_COMPLETED', 'completed', v_completed, 'total', v_total);
    END IF;

    -- Check if already claimed
    IF EXISTS (SELECT 1 FROM daily_challenge_claims WHERE user_id = p_user_id AND claim_date = v_today) THEN
        RETURN jsonb_build_object('success', false, 'error', 'ALREADY_CLAIMED');
    END IF;

    -- Record claim
    INSERT INTO daily_challenge_claims (user_id, claim_date, bonus_xp)
    VALUES (p_user_id, v_today, v_bonus_xp);

    -- Award bonus XP
    UPDATE users
    SET xp = xp + v_bonus_xp,
        total_xp = total_xp + v_bonus_xp
    WHERE id = p_user_id;

    RETURN jsonb_build_object('success', true, 'bonus_xp', v_bonus_xp, 'total_challenge_xp', v_completed * 15 + v_bonus_xp);
END;
$$;
