-- 20260720133238_create_portfolio_agent_readonly_role.sql
-- Description: Read-only DB-роль для Portfolio Analyst Agent (инвариант 6, §4.5 спеки).
-- Роль имеет SELECT на все существующие торговые таблицы + полный доступ (SELECT/INSERT/UPDATE)
-- ТОЛЬКО на 4 новые agent_* таблицы Phase 0. Никакого DELETE нигде, никакого доступа к
-- schema_migrations (защита от случайной порчи миграционного состояния).
--
-- Применять ПОСЛЕ миграции создания agent_* таблиц (иначе GRANT на них упадёт — таблиц ещё нет).
-- Пароль роли задаётся отдельно вручную на проде через ALTER ROLE ... WITH PASSWORD
-- (НЕ хранить пароль в этом файле — миграции коммитятся в git).

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'oraclepoly_agent_ro') THEN
        CREATE ROLE oraclepoly_agent_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END
$$;

-- Явно НЕ ставим пароль здесь — задаётся руками на проде отдельной командой,
-- чтобы пароль роли не попал в git history миграций:
--   ssh <your-server> "docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly \
--     -c \"ALTER ROLE oraclepoly_agent_ro WITH PASSWORD '<сгенерированный>';\""

-- Read-only на существующую схему (текущие и БУДУЩИЕ таблицы public — ALTER DEFAULT PRIVILEGES
-- покрывает таблицы, создаваемые последующими миграциями, без ручного повторного GRANT).
GRANT CONNECT ON DATABASE oraclepoly TO oraclepoly_agent_ro;
GRANT USAGE ON SCHEMA public TO oraclepoly_agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO oraclepoly_agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO oraclepoly_agent_ro;

-- Явный запрет на schema_migrations (защита миграционного состояния от случайного чтения-и-логики-на-нём).
-- SELECT уже выдан широким GRANT выше — отзываем точечно.
REVOKE ALL ON schema_migrations FROM oraclepoly_agent_ro;

-- Полный CRUD (без DELETE) на 4 agent_* таблицы Phase 0 — сюда агент пишет свои прогоны/клеймы.
GRANT SELECT, INSERT, UPDATE ON agent_claims, agent_runs, agent_actions, agent_llm_calls TO oraclepoly_agent_ro;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO oraclepoly_agent_ro;
