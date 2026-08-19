-- Drop legacy objects (Phase C.3.5)
--
-- Перед созданием phoenix snapshot чистим legacy:
--   - User-фичи функции (referrals, gamification, votes, credits)
--   - Triggers и schemas от удалённых таблиц
--   - cleanup_backup_20260424_live schema (старый backup)
--   - Supabase auth schema (не используется)

BEGIN;

-- User feature functions (referrals, XP, gamification, votes, credits)
DROP FUNCTION IF EXISTS public.activate_referral(uuid);
DROP FUNCTION IF EXISTS public.award_category_xp(uuid, uuid, integer);
DROP FUNCTION IF EXISTS public.cast_vote_atomic(bigint, text, text, integer, integer, integer);
DROP FUNCTION IF EXISTS public.deduct_credit(bigint, integer);
DROP FUNCTION IF EXISTS public.get_cumulative_xp_for_level(integer);
DROP FUNCTION IF EXISTS public.get_mastery_level(integer);
DROP FUNCTION IF EXISTS public.get_user_category_rank(uuid, text);
DROP FUNCTION IF EXISTS public.get_user_predictions(uuid);
DROP FUNCTION IF EXISTS public.get_user_rank_by_period(text);
DROP FUNCTION IF EXISTS public.get_xp_for_next_level(integer);
DROP FUNCTION IF EXISTS public.grant_credits(bigint, integer);
DROP FUNCTION IF EXISTS public.process_referral(uuid, uuid);
DROP FUNCTION IF EXISTS public.redeem_xp_for_credits(uuid, integer);
DROP FUNCTION IF EXISTS public.update_payments_updated_at();

-- WebSocket broadcast trigger (frontend убран)
DROP FUNCTION IF EXISTS public.notify_table_change() CASCADE;

-- Cleanup backup schema (старый backup от 24 апреля 2026)
DROP SCHEMA IF EXISTS cleanup_backup_20260424_live CASCADE;

-- Supabase auth schema (legacy, не используется в self-trading bot)
DROP SCHEMA IF EXISTS auth CASCADE;

COMMIT;
