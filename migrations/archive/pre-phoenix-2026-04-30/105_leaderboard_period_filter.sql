-- ============================================
-- Migration 105: Leaderboard period filter
-- 1. Recreate top_analysts with XP >= 100 filter
-- 2. Add get_leaderboard_by_period RPC
-- 3. Add get_user_rank_by_period RPC
-- ============================================

-- 1. Recreate materialized view with XP threshold
DROP MATERIALIZED VIEW IF EXISTS top_analysts CASCADE;

CREATE MATERIALIZED VIEW top_analysts AS
SELECT
    u.id,
    u.telegram_id,
    u.username,
    u.display_name,
    u.avatar_url,
    u.total_xp,
    u.daily_streak,
    u.level,
    COUNT(cv.id)::int AS total_votes,
    COUNT(cv.id) FILTER (WHERE cv.rewarded = true)::int AS correct_votes,
    CASE
        WHEN COUNT(cv.id) > 0 THEN ROUND(COUNT(cv.id) FILTER (WHERE cv.rewarded = true)::numeric / COUNT(cv.id), 4)
        ELSE 0
    END AS accuracy_rate,
    ROW_NUMBER() OVER (ORDER BY u.total_xp DESC) AS global_rank
FROM users u
LEFT JOIN consensus_votes cv ON u.id = cv.user_id
WHERE u.total_xp >= 100
GROUP BY u.id, u.telegram_id, u.username, u.display_name, u.avatar_url, u.total_xp, u.daily_streak, u.level
ORDER BY u.total_xp DESC;

CREATE UNIQUE INDEX idx_top_analysts_id ON top_analysts(id);
CREATE INDEX idx_top_analysts_rank ON top_analysts(global_rank);

-- Recreate refresh function
CREATE OR REPLACE FUNCTION refresh_top_analysts() RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY top_analysts;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- 2. Leaderboard by period RPC
CREATE OR REPLACE FUNCTION get_leaderboard_by_period(
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
    IF p_period = 'all' THEN
        -- Use materialized view for all-time
        SELECT COUNT(*) INTO v_total FROM top_analysts;

        RETURN QUERY
        SELECT
            ta.global_rank::bigint AS rank,
            ta.telegram_id::bigint,
            ta.username::text,
            ta.display_name::text,
            ta.avatar_url::text,
            ta.total_xp::bigint,
            ta.total_xp::bigint AS period_xp,
            ta.daily_streak::int,
            ta.total_votes::bigint,
            ta.correct_votes::bigint,
            ta.accuracy_rate::numeric,
            v_total AS total_count
        FROM top_analysts ta
        ORDER BY ta.global_rank
        LIMIT p_limit OFFSET p_offset;
    ELSE
        -- Live query for weekly/monthly
        IF p_period = 'weekly' THEN
            v_since := NOW() - INTERVAL '7 days';
        ELSE
            v_since := NOW() - INTERVAL '30 days';
        END IF;

        -- Count users who qualify
        SELECT COUNT(*) INTO v_total
        FROM (
            SELECT u.id
            FROM users u
            INNER JOIN consensus_votes cv ON u.id = cv.user_id AND cv.created_at >= v_since
            WHERE u.total_xp >= 100
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
        INNER JOIN consensus_votes cv ON u.id = cv.user_id AND cv.created_at >= v_since
        WHERE u.total_xp >= 100
        GROUP BY u.id, u.telegram_id, u.username, u.display_name, u.avatar_url, u.total_xp, u.daily_streak
        ORDER BY period_xp DESC
        LIMIT p_limit OFFSET p_offset;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- 3. User rank by period RPC
CREATE OR REPLACE FUNCTION get_user_rank_by_period(
    p_telegram_id BIGINT,
    p_period TEXT DEFAULT 'all'
)
RETURNS TABLE(rank BIGINT) AS $$
DECLARE
    v_since TIMESTAMPTZ;
BEGIN
    IF p_period = 'all' THEN
        RETURN QUERY
        SELECT ta.global_rank::bigint AS rank
        FROM top_analysts ta
        WHERE ta.telegram_id = p_telegram_id;
    ELSE
        IF p_period = 'weekly' THEN
            v_since := NOW() - INTERVAL '7 days';
        ELSE
            v_since := NOW() - INTERVAL '30 days';
        END IF;

        RETURN QUERY
        SELECT sub.rank FROM (
            SELECT
                u.telegram_id,
                ROW_NUMBER() OVER (ORDER BY COALESCE(SUM(cv.xp_earned), 0) DESC)::bigint AS rank
            FROM users u
            INNER JOIN consensus_votes cv ON u.id = cv.user_id AND cv.created_at >= v_since
            WHERE u.total_xp >= 100
            GROUP BY u.id, u.telegram_id
        ) sub
        WHERE sub.telegram_id = p_telegram_id;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
