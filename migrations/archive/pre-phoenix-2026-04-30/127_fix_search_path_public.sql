-- Migration 127: Fix search_path on all public functions
--
-- Migration 122 set search_path = '' on all functions, but the function bodies
-- use unqualified table references (users, consensus_votes, etc.).
-- Empty search_path means PostgreSQL cannot resolve these names.
-- Fix: set search_path = 'public' — still explicit (secure), but allows
-- unqualified references to resolve against the public schema.

ALTER FUNCTION public.activate_referral(uuid) SET search_path = 'public';
ALTER FUNCTION public.award_category_xp(uuid, uuid, integer) SET search_path = 'public';
ALTER FUNCTION public.check_achievements(uuid) SET search_path = 'public';
ALTER FUNCTION public.check_daily_challenges(uuid, text, jsonb) SET search_path = 'public';
ALTER FUNCTION public.claim_daily_challenge_bonus(uuid) SET search_path = 'public';
ALTER FUNCTION public.claim_daily_mission_reward(uuid, integer, integer) SET search_path = 'public';
ALTER FUNCTION public.deduct_credit(bigint, integer) SET search_path = 'public';
ALTER FUNCTION public.generate_daily_challenges(uuid) SET search_path = 'public';
ALTER FUNCTION public.get_category_leaderboard(text, text, integer, integer) SET search_path = 'public';
ALTER FUNCTION public.get_cumulative_xp_for_level(integer) SET search_path = 'public';
ALTER FUNCTION public.get_leaderboard_by_period(text, integer, integer) SET search_path = 'public';
ALTER FUNCTION public.get_level_perks(integer) SET search_path = 'public';
ALTER FUNCTION public.get_mastery_level(integer) SET search_path = 'public';
ALTER FUNCTION public.get_user_category_rank(bigint, text, text) SET search_path = 'public';
ALTER FUNCTION public.get_user_rank_by_period(bigint, text) SET search_path = 'public';
ALTER FUNCTION public.get_xp_for_next_level(integer) SET search_path = 'public';
ALTER FUNCTION public.grant_credits(bigint, integer) SET search_path = 'public';
ALTER FUNCTION public.process_referral(bigint, uuid) SET search_path = 'public';
ALTER FUNCTION public.redeem_xp_for_credits(uuid, integer) SET search_path = 'public';
ALTER FUNCTION public.refresh_top_analysts() SET search_path = 'public';
ALTER FUNCTION public.update_payments_updated_at() SET search_path = 'public';
ALTER FUNCTION public.cast_vote_atomic(bigint, text, text, integer, integer, integer) SET search_path = 'public';
