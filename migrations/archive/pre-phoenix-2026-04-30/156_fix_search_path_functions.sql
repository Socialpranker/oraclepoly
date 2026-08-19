-- Фикс: все SECURITY DEFINER функции имели search_path="" (пустой),
-- из-за чего не могли видеть таблицы в schema public.
-- Ошибка: "relation "users" does not exist" при вызове deduct_credit() и т.д.

ALTER FUNCTION deduct_credit(bigint, integer) SET search_path = public;
ALTER FUNCTION grant_credits(bigint, integer) SET search_path = public;
ALTER FUNCTION cast_vote_atomic(bigint, text, text, integer, integer, integer) SET search_path = public;
ALTER FUNCTION check_achievements(uuid) SET search_path = public;
ALTER FUNCTION check_daily_challenges(uuid, text, jsonb) SET search_path = public;
ALTER FUNCTION claim_daily_challenge_bonus(uuid) SET search_path = public;
ALTER FUNCTION claim_daily_mission_reward(uuid, integer, integer) SET search_path = public;
ALTER FUNCTION generate_daily_challenges(uuid) SET search_path = public;
ALTER FUNCTION get_category_leaderboard(text, text, integer, integer) SET search_path = public;
ALTER FUNCTION get_leaderboard_by_period(text, integer, integer) SET search_path = public;
ALTER FUNCTION get_user_category_rank(bigint, text, text) SET search_path = public;
ALTER FUNCTION get_user_rank_by_period(bigint, text) SET search_path = public;
ALTER FUNCTION activate_referral(uuid) SET search_path = public;
ALTER FUNCTION process_referral(bigint, uuid) SET search_path = public;
ALTER FUNCTION award_category_xp(uuid, uuid, integer) SET search_path = public;
ALTER FUNCTION redeem_xp_for_credits(uuid, integer) SET search_path = public;
ALTER FUNCTION refresh_top_analysts() SET search_path = public;
