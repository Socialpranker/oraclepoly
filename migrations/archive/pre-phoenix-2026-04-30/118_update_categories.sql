-- ============================================
-- Migration 118: Unified 6-Category System
--
-- Reclassifies "General" markets using keyword matching,
-- updates achievement conditions, and updates daily challenges
-- to use the full 6-category set.
-- ============================================

-- 1. Reclassify existing "General" markets via keyword inference
-- Tech & AI
UPDATE markets SET category = 'Tech & AI'
WHERE category = 'General'
  AND (
    lower(question || ' ' || COALESCE(description, '')) ~ '(ai|artificial intelligence|openai|chatgpt|gpt|claude|google|apple|microsoft|meta|nvidia|tesla|spacex|elon musk|robot|autonomous|machine learning|semiconductor|chip|tech|software|startup|ipo)'
  );

-- Culture
UPDATE markets SET category = 'Culture'
WHERE category = 'General'
  AND (
    lower(question || ' ' || COALESCE(description, '')) ~ '(movie|film|oscar|grammy|emmy|music|album|celebrity|award|box office|tv show|series|netflix|disney|streaming|entertainment|concert|festival|taylor swift|beyonce|drake)'
  );

-- World & Economy (remaining General markets with geopolitical/economic keywords)
UPDATE markets SET category = 'World & Economy'
WHERE category = 'General'
  AND (
    lower(question || ' ' || COALESCE(description, '')) ~ '(war|ukraine|russia|china|israel|gaza|iran|nato|sanction|gdp|inflation|fed|interest rate|recession|unemployment|oil|commodity|trade|tariff|economy|economic|geopolitical|ceasefire|diplomatic|ambassador)'
  );

-- 2. Update achievement: all_categories condition from mastery_all_3 to mastery_all_6
UPDATE achievements_config
SET condition_type = 'mastery_all_6', condition_value = 1
WHERE id = 'all_categories';

-- 3. Replace generate_daily_challenges — update fallback category array to all 6
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
        v_categories := ARRAY['Crypto', 'Politics', 'Sports', 'Tech & AI', 'Culture', 'World & Economy'];
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
