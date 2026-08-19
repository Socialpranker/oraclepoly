-- Migration 116: Category Leaderboard
-- Provides per-category ranking based on XP earned in each category

CREATE OR REPLACE FUNCTION get_category_leaderboard(
    p_category TEXT,
    p_period TEXT DEFAULT 'all',
    p_limit INT DEFAULT 50,
    p_offset INT DEFAULT 0
)
RETURNS TABLE(
    rank BIGINT,
    telegram_id BIGINT,
    username TEXT,
    display_name TEXT,
    avatar_url TEXT,
    total_xp BIGINT,
    period_xp BIGINT,
    daily_streak INT,
    total_votes BIGINT,
    correct_votes BIGINT,
    accuracy_rate NUMERIC,
    total_count BIGINT
) AS $$
DECLARE
    v_since TIMESTAMPTZ;
    v_total BIGINT;
BEGIN
    -- Determine time window
    IF p_period = 'weekly' THEN
        v_since := NOW() - INTERVAL '7 days';
    ELSIF p_period = 'monthly' THEN
        v_since := NOW() - INTERVAL '30 days';
    ELSE
        v_since := NULL; -- all time
    END IF;

    -- Count qualifying users
    SELECT COUNT(*) INTO v_total
    FROM (
        SELECT u.id
        FROM users u
        INNER JOIN consensus_votes cv ON u.id = cv.user_id
        INNER JOIN markets m ON m.id::text = cv.market_id
        WHERE m.category = p_category
          AND (v_since IS NULL OR cv.created_at >= v_since)
          AND u.total_xp >= 100
        GROUP BY u.id
    ) sub;

    RETURN QUERY
    SELECT
        ROW_NUMBER() OVER (ORDER BY COALESCE(SUM(cv.xp_earned), 0) DESC)::bigint AS rank,
        u.telegram_id::bigint,
        u.username::text,
        u.display_name::text,
        u.avatar_url::text,
        u.total_xp::bigint,
        COALESCE(SUM(cv.xp_earned), 0)::bigint AS period_xp,
        u.daily_streak::int,
        COUNT(cv.id)::bigint AS total_votes,
        COUNT(cv.id) FILTER (WHERE cv.rewarded = true)::bigint AS correct_votes,
        CASE
            WHEN COUNT(cv.id) > 0
            THEN ROUND(COUNT(cv.id) FILTER (WHERE cv.rewarded = true)::numeric / COUNT(cv.id), 4)
            ELSE 0
        END AS accuracy_rate,
        v_total AS total_count
    FROM users u
    INNER JOIN consensus_votes cv ON u.id = cv.user_id
    INNER JOIN markets m ON m.id::text = cv.market_id
    WHERE m.category = p_category
      AND (v_since IS NULL OR cv.created_at >= v_since)
      AND u.total_xp >= 100
    GROUP BY u.id, u.telegram_id, u.username, u.display_name, u.avatar_url, u.total_xp, u.daily_streak
    ORDER BY period_xp DESC
    LIMIT p_limit OFFSET p_offset;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- User rank in a specific category
CREATE OR REPLACE FUNCTION get_user_category_rank(
    p_telegram_id BIGINT,
    p_category TEXT,
    p_period TEXT DEFAULT 'all'
)
RETURNS TABLE(rank BIGINT) AS $$
DECLARE
    v_since TIMESTAMPTZ;
BEGIN
    IF p_period = 'weekly' THEN
        v_since := NOW() - INTERVAL '7 days';
    ELSIF p_period = 'monthly' THEN
        v_since := NOW() - INTERVAL '30 days';
    ELSE
        v_since := NULL;
    END IF;

    RETURN QUERY
    SELECT sub.rank FROM (
        SELECT
            u.telegram_id,
            ROW_NUMBER() OVER (ORDER BY COALESCE(SUM(cv.xp_earned), 0) DESC)::bigint AS rank
        FROM users u
        INNER JOIN consensus_votes cv ON u.id = cv.user_id
        INNER JOIN markets m ON m.id::text = cv.market_id
        WHERE m.category = p_category
          AND (v_since IS NULL OR cv.created_at >= v_since)
          AND u.total_xp >= 100
        GROUP BY u.id, u.telegram_id
    ) sub
    WHERE sub.telegram_id = p_telegram_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
