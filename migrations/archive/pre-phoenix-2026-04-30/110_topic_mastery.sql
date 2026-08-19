-- Migration 110: Topic mastery — per-category XP tracking

-- 1. User category mastery table
CREATE TABLE IF NOT EXISTS user_category_mastery (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    xp INTEGER DEFAULT 0,
    level INTEGER DEFAULT 1,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, category)
);

-- 2. Market categories cache (markets table already has 'category' column)
-- No separate table needed — we use markets.category directly

-- 3. Mastery level thresholds: 0/100/300/600/1000
-- Titles: Novice / Apprentice / Analyst / Expert / Master

-- 4. Function to get mastery level from XP
CREATE OR REPLACE FUNCTION get_mastery_level(p_xp INT)
RETURNS INT
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN CASE
        WHEN p_xp >= 1000 THEN 5
        WHEN p_xp >= 600  THEN 4
        WHEN p_xp >= 300  THEN 3
        WHEN p_xp >= 100  THEN 2
        ELSE 1
    END;
END;
$$;

-- 5. Function to award category XP when voting
CREATE OR REPLACE FUNCTION award_category_xp(
    p_user_id UUID,
    p_market_id UUID,
    p_xp INT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_category TEXT;
    v_new_xp INT;
    v_new_level INT;
BEGIN
    -- Get category from market
    SELECT category INTO v_category
    FROM markets WHERE id = p_market_id;

    IF v_category IS NULL OR v_category = '' THEN
        v_category := 'General';
    END IF;

    -- Upsert category mastery
    INSERT INTO user_category_mastery (user_id, category, xp, level, updated_at)
    VALUES (p_user_id, v_category, p_xp, get_mastery_level(p_xp), NOW())
    ON CONFLICT (user_id, category)
    DO UPDATE SET
        xp = user_category_mastery.xp + p_xp,
        level = get_mastery_level(user_category_mastery.xp + p_xp),
        updated_at = NOW();
END;
$$;
