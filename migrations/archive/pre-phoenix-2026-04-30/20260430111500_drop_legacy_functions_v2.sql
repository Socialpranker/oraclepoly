-- Drop legacy functions v2 (правильные сигнатуры)

BEGIN;

DROP FUNCTION IF EXISTS public.get_user_category_rank(bigint, text, text);
DROP FUNCTION IF EXISTS public.get_user_predictions(uuid, text, integer, integer);
DROP FUNCTION IF EXISTS public.get_user_rank_by_period(bigint, text);
DROP FUNCTION IF EXISTS public.process_referral(bigint, uuid);

COMMIT;
