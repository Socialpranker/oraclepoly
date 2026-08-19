-- Migration 122: Fix all Supabase security advisors
--
-- 1. Enable RLS on 5 unprotected tables + add policies
-- 2. Set search_path on all 24 public functions
-- 3. Drop 2 stale cast_vote_atomic overloads (superseded by TEXT version in 113)

-- ============================================
-- 1. RLS on unprotected tables
-- ============================================

-- payments: sensitive — service_role only
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON payments FOR ALL USING (auth.role() = 'service_role');

-- user_subscriptions: sensitive — service_role only
ALTER TABLE user_subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON user_subscriptions FOR ALL USING (auth.role() = 'service_role');

-- subscription_plans: public read, service_role write
ALTER TABLE subscription_plans ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow public read" ON subscription_plans FOR SELECT USING (true);
CREATE POLICY "Service role full access" ON subscription_plans FOR ALL USING (auth.role() = 'service_role');

-- credit_packs: public read, service_role write
ALTER TABLE credit_packs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow public read" ON credit_packs FOR SELECT USING (true);
CREATE POLICY "Service role full access" ON credit_packs FOR ALL USING (auth.role() = 'service_role');

-- user_market_unlocks: service_role only
ALTER TABLE user_market_unlocks ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON user_market_unlocks FOR ALL USING (auth.role() = 'service_role');

-- ============================================
-- 2. Fix search_path on all public functions
-- ============================================

ALTER FUNCTION public.activate_referral(uuid) SET search_path = '';
ALTER FUNCTION public.award_category_xp(uuid, uuid, integer) SET search_path = '';
ALTER FUNCTION public.check_achievements(uuid) SET search_path = '';
ALTER FUNCTION public.check_daily_challenges(uuid, text, jsonb) SET search_path = '';
ALTER FUNCTION public.claim_daily_challenge_bonus(uuid) SET search_path = '';
ALTER FUNCTION public.claim_daily_mission_reward(uuid, integer, integer) SET search_path = '';
ALTER FUNCTION public.deduct_credit(bigint, integer) SET search_path = '';
ALTER FUNCTION public.generate_daily_challenges(uuid) SET search_path = '';
ALTER FUNCTION public.get_category_leaderboard(text, text, integer, integer) SET search_path = '';
ALTER FUNCTION public.get_cumulative_xp_for_level(integer) SET search_path = '';
ALTER FUNCTION public.get_leaderboard_by_period(text, integer, integer) SET search_path = '';
ALTER FUNCTION public.get_level_perks(integer) SET search_path = '';
ALTER FUNCTION public.get_mastery_level(integer) SET search_path = '';
ALTER FUNCTION public.get_user_category_rank(bigint, text, text) SET search_path = '';
ALTER FUNCTION public.get_user_rank_by_period(bigint, text) SET search_path = '';
ALTER FUNCTION public.get_xp_for_next_level(integer) SET search_path = '';
ALTER FUNCTION public.grant_credits(bigint, integer) SET search_path = '';
ALTER FUNCTION public.process_referral(bigint, uuid) SET search_path = '';
ALTER FUNCTION public.redeem_xp_for_credits(uuid, integer) SET search_path = '';
ALTER FUNCTION public.refresh_top_analysts() SET search_path = '';
ALTER FUNCTION public.update_payments_updated_at() SET search_path = '';

-- Current active version (TEXT market_id, from migration 113)
ALTER FUNCTION public.cast_vote_atomic(bigint, text, text, integer, integer, integer) SET search_path = '';

-- ============================================
-- 3. Drop stale cast_vote_atomic overloads
--    Only the TEXT version (migration 113) is used.
--    The UUID overloads from migrations 103/106 are dead.
-- ============================================
DROP FUNCTION IF EXISTS public.cast_vote_atomic(bigint, uuid, text, integer, integer);
DROP FUNCTION IF EXISTS public.cast_vote_atomic(bigint, uuid, text, integer, integer, integer);
