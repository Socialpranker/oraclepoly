-- Drop user-facing tables (Phase B.9)
--
-- После strip-down до self-trading bot эти таблицы больше не используются.
-- Удаляем CASCADE т.к. есть FK от других удаляемых таблиц.

BEGIN;

-- User accounts + related
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS user_alert_rules CASCADE;
DROP TABLE IF EXISTS user_alert_preferences CASCADE;
DROP TABLE IF EXISTS user_market_unlocks CASCADE;
DROP TABLE IF EXISTS user_category_mastery CASCADE;
DROP TABLE IF EXISTS user_subscriptions CASCADE;

-- Subscriptions / billing
DROP TABLE IF EXISTS subscription_plans CASCADE;
DROP TABLE IF EXISTS payments CASCADE;
DROP TABLE IF EXISTS credit_packs CASCADE;

-- Notifications
DROP TABLE IF EXISTS notifications CASCADE;

-- Voting (consensus / market predictions)
DROP TABLE IF EXISTS vote_snapshots CASCADE;
DROP TABLE IF EXISTS consensus_votes CASCADE;

-- Whale follows (user feature)
DROP TABLE IF EXISTS whale_follows CASCADE;

COMMIT;
