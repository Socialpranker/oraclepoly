-- 178: Отключить RLS на всех таблицах (Supabase legacy)
--
-- После миграции Supabase → asyncpg (PR #107):
-- - auth.role() / auth.uid() не существуют в vanilla PostgreSQL
-- - oraclepoly user — владелец таблиц, RLS обходится автоматически
-- - Политики — мёртвый код, но засоряют pg_policy и могут сломаться при смене роли
--
-- Бэкенд авторизует через Telegram init data (x-telegram-init-data header),
-- PostgreSQL не участвует в аутентификации.

-- Основные таблицы (001_initial_schema)
ALTER TABLE whales DISABLE ROW LEVEL SECURITY;
ALTER TABLE transactions DISABLE ROW LEVEL SECURITY;
ALTER TABLE markets DISABLE ROW LEVEL SECURITY;
ALTER TABLE news DISABLE ROW LEVEL SECURITY;
ALTER TABLE news_market_mappings DISABLE ROW LEVEL SECURITY;

-- Users & gamification (004, 005)
ALTER TABLE users DISABLE ROW LEVEL SECURITY;
ALTER TABLE consensus_votes DISABLE ROW LEVEL SECURITY;

-- Payments (131)
ALTER TABLE payments DISABLE ROW LEVEL SECURITY;
ALTER TABLE user_subscriptions DISABLE ROW LEVEL SECURITY;
ALTER TABLE subscription_plans DISABLE ROW LEVEL SECURITY;
ALTER TABLE credit_packs DISABLE ROW LEVEL SECURITY;
ALTER TABLE user_market_unlocks DISABLE ROW LEVEL SECURITY;

-- Events & analyses (125)
ALTER TABLE events DISABLE ROW LEVEL SECURITY;
ALTER TABLE market_analyses DISABLE ROW LEVEL SECURITY;

-- Vote snapshots (132)
ALTER TABLE vote_snapshots DISABLE ROW LEVEL SECURITY;

-- Gamification tables (109, 110, 112, 113, 114, 115)
-- Используем DO block чтобы не падать если таблица не существует
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN SELECT unnest(ARRAY[
        'user_achievements', 'user_category_mastery',
        'daily_challenges', 'daily_challenge_claims',
        'level_perks', 'xp_transactions', 'referrals',
        'achievements_config'
    ]) LOOP
        EXECUTE format('ALTER TABLE IF EXISTS %I DISABLE ROW LEVEL SECURITY', tbl);
    END LOOP;
END $$;

-- Удалить все мёртвые RLS-политики (auth.role / auth.uid)
-- Политики остаются в pg_policy даже после DISABLE RLS, чистим
DO $$
DECLARE
    pol RECORD;
BEGIN
    FOR pol IN
        SELECT policyname, tablename
        FROM pg_policies
        WHERE schemaname = 'public'
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', pol.policyname, pol.tablename);
    END LOOP;
END $$;
