-- Phase E: Gamification tables + CWI index cleanup
-- Dead tables: user_achievements, xp_transactions, achievements_config,
--              daily_challenges, level_perks (0 rows, no INSERTs in app code)
-- Dead DB functions: check_daily_challenges, generate_daily_challenges,
--                    get_level_perks, refresh_top_analysts
-- Dead index: idx_markets_cwi_regime (cwi_recalculate cron dead since 2026-03-23)

-- 1. Drop dead index
DROP INDEX IF EXISTS idx_markets_cwi_regime;

-- 2. Drop dead gamification tables
DROP TABLE IF EXISTS user_achievements CASCADE;
DROP TABLE IF EXISTS xp_transactions CASCADE;
DROP TABLE IF EXISTS achievements_config CASCADE;
DROP TABLE IF EXISTS daily_challenges CASCADE;
DROP TABLE IF EXISTS level_perks CASCADE;

-- 3. Drop dead DB functions
DROP FUNCTION IF EXISTS check_daily_challenges(uuid, text, text);
DROP FUNCTION IF EXISTS generate_daily_challenges(uuid);
DROP FUNCTION IF EXISTS get_level_perks(integer);
DROP FUNCTION IF EXISTS refresh_top_analysts();
