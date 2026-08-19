-- Migration 123: Fix all Supabase performance advisors
--
-- 1. Recreate RLS policies with (select auth.role()) to avoid per-row re-evaluation
-- 2. Eliminate multiple permissive policies overlap on SELECT
-- 3. Drop duplicate constraint on consensus_votes
-- 4. Add missing FK indexes

-- ============================================
-- Helper: wrap policy recreation in DO block
-- ============================================

DO $$
BEGIN

-- =============================================
-- TABLES WITH PUBLIC READ + SERVICE ROLE WRITE
-- Pattern: public SELECT + service_role INSERT/UPDATE/DELETE
-- =============================================

-- whales
DROP POLICY IF EXISTS "Allow public read" ON whales;
DROP POLICY IF EXISTS "Service role full access" ON whales;
CREATE POLICY "Allow public read" ON whales FOR SELECT USING (true);
CREATE POLICY "Service role write" ON whales FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON whales FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON whales FOR DELETE USING ((select auth.role()) = 'service_role');

-- transactions
DROP POLICY IF EXISTS "Allow public read" ON transactions;
DROP POLICY IF EXISTS "Service role full access" ON transactions;
CREATE POLICY "Allow public read" ON transactions FOR SELECT USING (true);
CREATE POLICY "Service role write" ON transactions FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON transactions FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON transactions FOR DELETE USING ((select auth.role()) = 'service_role');

-- markets
DROP POLICY IF EXISTS "Allow public read" ON markets;
DROP POLICY IF EXISTS "Service role full access" ON markets;
CREATE POLICY "Allow public read" ON markets FOR SELECT USING (true);
CREATE POLICY "Service role write" ON markets FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON markets FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON markets FOR DELETE USING ((select auth.role()) = 'service_role');

-- news
DROP POLICY IF EXISTS "Allow public read" ON news;
DROP POLICY IF EXISTS "Service role full access" ON news;
CREATE POLICY "Allow public read" ON news FOR SELECT USING (true);
CREATE POLICY "Service role write" ON news FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON news FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON news FOR DELETE USING ((select auth.role()) = 'service_role');

-- news_market_mappings
DROP POLICY IF EXISTS "Allow public read" ON news_market_mappings;
DROP POLICY IF EXISTS "Service role full access" ON news_market_mappings;
CREATE POLICY "Allow public read" ON news_market_mappings FOR SELECT USING (true);
CREATE POLICY "Service role write" ON news_market_mappings FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON news_market_mappings FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON news_market_mappings FOR DELETE USING ((select auth.role()) = 'service_role');

-- black_swan_scenarios
DROP POLICY IF EXISTS "Allow public read" ON black_swan_scenarios;
DROP POLICY IF EXISTS "Service role full access" ON black_swan_scenarios;
CREATE POLICY "Allow public read" ON black_swan_scenarios FOR SELECT USING (true);
CREATE POLICY "Service role write" ON black_swan_scenarios FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON black_swan_scenarios FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON black_swan_scenarios FOR DELETE USING ((select auth.role()) = 'service_role');

-- subscription_plans
DROP POLICY IF EXISTS "Allow public read" ON subscription_plans;
DROP POLICY IF EXISTS "Service role full access" ON subscription_plans;
CREATE POLICY "Allow public read" ON subscription_plans FOR SELECT USING (true);
CREATE POLICY "Service role write" ON subscription_plans FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON subscription_plans FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON subscription_plans FOR DELETE USING ((select auth.role()) = 'service_role');

-- credit_packs
DROP POLICY IF EXISTS "Allow public read" ON credit_packs;
DROP POLICY IF EXISTS "Service role full access" ON credit_packs;
CREATE POLICY "Allow public read" ON credit_packs FOR SELECT USING (true);
CREATE POLICY "Service role write" ON credit_packs FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON credit_packs FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON credit_packs FOR DELETE USING ((select auth.role()) = 'service_role');

-- achievements_config (has "Public read access" name variant)
DROP POLICY IF EXISTS "Public read access" ON achievements_config;
DROP POLICY IF EXISTS "Service role full access" ON achievements_config;
CREATE POLICY "Allow public read" ON achievements_config FOR SELECT USING (true);
CREATE POLICY "Service role write" ON achievements_config FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON achievements_config FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON achievements_config FOR DELETE USING ((select auth.role()) = 'service_role');

-- level_perks (has "Public read access" name variant)
DROP POLICY IF EXISTS "Public read access" ON level_perks;
DROP POLICY IF EXISTS "Service role full access" ON level_perks;
CREATE POLICY "Allow public read" ON level_perks FOR SELECT USING (true);
CREATE POLICY "Service role write" ON level_perks FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON level_perks FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON level_perks FOR DELETE USING ((select auth.role()) = 'service_role');

-- =============================================
-- TABLES WITH USER-SPECIFIC READ + SERVICE ROLE
-- =============================================

-- users
DROP POLICY IF EXISTS "Users can read own data" ON users;
DROP POLICY IF EXISTS "Service role full access" ON users;
CREATE POLICY "Users can read own data" ON users FOR SELECT
    USING ((select auth.uid())::text = telegram_id::text OR (select auth.role()) = 'service_role');
CREATE POLICY "Service role write" ON users FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update" ON users FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete" ON users FOR DELETE USING ((select auth.role()) = 'service_role');

-- consensus_votes
DROP POLICY IF EXISTS "Users can read own votes" ON consensus_votes;
DROP POLICY IF EXISTS "Service role full access on votes" ON consensus_votes;
CREATE POLICY "Users can read own votes" ON consensus_votes FOR SELECT
    USING (
        user_id IN (SELECT id FROM users WHERE telegram_id::text = (select auth.uid())::text)
        OR (select auth.role()) = 'service_role'
    );
CREATE POLICY "Service role write votes" ON consensus_votes FOR INSERT WITH CHECK ((select auth.role()) = 'service_role');
CREATE POLICY "Service role update votes" ON consensus_votes FOR UPDATE USING ((select auth.role()) = 'service_role');
CREATE POLICY "Service role delete votes" ON consensus_votes FOR DELETE USING ((select auth.role()) = 'service_role');

-- =============================================
-- TABLES WITH SERVICE ROLE ONLY (fix auth.role() -> (select auth.role()))
-- =============================================

DROP POLICY IF EXISTS "Service role full access" ON payments;
CREATE POLICY "Service role full access" ON payments FOR ALL USING ((select auth.role()) = 'service_role');

DROP POLICY IF EXISTS "Service role full access" ON user_subscriptions;
CREATE POLICY "Service role full access" ON user_subscriptions FOR ALL USING ((select auth.role()) = 'service_role');

DROP POLICY IF EXISTS "Service role full access" ON user_market_unlocks;
CREATE POLICY "Service role full access" ON user_market_unlocks FOR ALL USING ((select auth.role()) = 'service_role');

DROP POLICY IF EXISTS "Service role full access" ON user_achievements;
CREATE POLICY "Service role full access" ON user_achievements FOR ALL USING ((select auth.role()) = 'service_role');

DROP POLICY IF EXISTS "Service role full access" ON user_category_mastery;
CREATE POLICY "Service role full access" ON user_category_mastery FOR ALL USING ((select auth.role()) = 'service_role');

DROP POLICY IF EXISTS "Service role full access" ON user_watchlist;
CREATE POLICY "Service role full access" ON user_watchlist FOR ALL USING ((select auth.role()) = 'service_role');

DROP POLICY IF EXISTS "Service role full access" ON daily_challenges;
CREATE POLICY "Service role full access" ON daily_challenges FOR ALL USING ((select auth.role()) = 'service_role');

DROP POLICY IF EXISTS "Service role full access" ON daily_challenge_claims;
CREATE POLICY "Service role full access" ON daily_challenge_claims FOR ALL USING ((select auth.role()) = 'service_role');

DROP POLICY IF EXISTS "Service role full access" ON xp_transactions;
CREATE POLICY "Service role full access" ON xp_transactions FOR ALL USING ((select auth.role()) = 'service_role');

DROP POLICY IF EXISTS "Service role full access" ON referrals;
CREATE POLICY "Service role full access" ON referrals FOR ALL USING ((select auth.role()) = 'service_role');

END $$;

-- ============================================
-- 3. Drop duplicate constraint on consensus_votes
-- ============================================
ALTER TABLE consensus_votes DROP CONSTRAINT IF EXISTS uq_consensus_votes_user_market;

-- ============================================
-- 4. Add missing FK indexes
-- ============================================
CREATE INDEX IF NOT EXISTS idx_user_achievements_achievement ON user_achievements(achievement_id);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_plan ON user_subscriptions(plan_id);
