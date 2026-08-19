-- Drop dead tables: не используются ни одним компонентом приложения
-- Аудит проведён 2026-03-22: grep по всему codebase (app/, scripts/, tests/)

-- 1. crowd_wisdom_snapshots: CWI калькулятор удалён, cron job удалён, 0 SQL-ссылок в app/
DROP TABLE IF EXISTS crowd_wisdom_snapshots CASCADE;

-- 2. live_paper_positions: создана в миграции 176, но нет ни репозитория, ни job, ни API — 0 кода
DROP TABLE IF EXISTS live_paper_positions CASCADE;

-- 3. orderbook_snapshots: cron пишет, но get_orderbook_snapshots() имеет 0 callers (write-only sink)
DROP TABLE IF EXISTS orderbook_snapshots CASCADE;

-- 4. sampling_markets: cron пишет из CLOB API, но 0 SELECT в app/ (write-only sink)
DROP TABLE IF EXISTS sampling_markets CASCADE;

-- 5. wallet_pnl: goldsky_sync пишет, но 0 SELECT в app/ — дублирует whales.pnl_usd
DROP TABLE IF EXISTS wallet_pnl CASCADE;
