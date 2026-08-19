-- 181: Schema audit cleanup — дубликаты, избыточные индексы, типы
--
-- Полный аудит 2026-03-19:
-- - 17 индексов удалено (дубликаты UNIQUE + подмножества составных + boolean)
-- - 1 индекс восстановлен (from_address — удалён в 130, но используется smart_money)
-- - 4 float→numeric (consistency с migration 179)
-- - Стандартизация gen_random_uuid() (built-in, без extension)
--
-- Безопасность: все DROP INDEX IF EXISTS, ALTER IF EXISTS.
-- На проде таблицы <100K строк, блокировка мгновенная.

-- ═══════════════════════════════════════════════════════
-- 1. ДУБЛИКАТНЫЕ ИНДЕКСЫ (btree дублирует UNIQUE constraint)
-- ═══════════════════════════════════════════════════════

-- markets.polymarket_id: дублирует markets_polymarket_id_key UNIQUE
DROP INDEX IF EXISTS idx_markets_polymarket_id;

-- news.url: дублирует news_url_key UNIQUE
DROP INDEX IF EXISTS idx_news_url;

-- users.telegram_id: дублирует users_telegram_id_key UNIQUE
DROP INDEX IF EXISTS idx_users_telegram_id;

-- payments.merchant_tx_id: дублирует payments_merchant_tx_id_key UNIQUE
DROP INDEX IF EXISTS idx_payments_merchant_tx;

-- payments.cryptopay_invoice_id: дублирует payments_cryptopay_invoice_id_key UNIQUE
DROP INDEX IF EXISTS idx_payments_cryptopay_invoice;

-- wallet_mappings.proxy: дублирует wallet_mappings_proxy_key UNIQUE
-- (таблица может быть удалена миграцией 157)
DROP INDEX IF EXISTS idx_wallet_mappings_proxy;

-- wallet_mappings.eoa: дублирует wallet_mappings_eoa_proxy_key UNIQUE (eoa leading column)
DROP INDEX IF EXISTS idx_wallet_mappings_eoa;

-- ═══════════════════════════════════════════════════════
-- 2. ИЗБЫТОЧНЫЕ ИНДЕКСЫ (подмножество составного с тем же leading column)
-- ═══════════════════════════════════════════════════════

-- transactions.whale_id: покрыт idx_transactions_whale_created (whale_id, created_at DESC)
DROP INDEX IF EXISTS idx_transactions_whale_id;

-- transactions.market_id: покрыт idx_transactions_market_created (market_id, created_at DESC)
DROP INDEX IF EXISTS idx_transactions_market_id;

-- consensus_votes.user_id: покрыт idx_consensus_votes_user_date (user_id, created_at)
DROP INDEX IF EXISTS idx_consensus_votes_user_id;

-- news_market_mappings.market_id: покрыт idx_news_mappings_market_created (market_id, created_at DESC)
DROP INDEX IF EXISTS idx_news_market_mappings_market_id;

-- black_swan_scenarios.market_id: покрыт idx_black_swan_market_scenario UNIQUE (market_id, scenario_title)
-- (таблица может быть удалена миграцией 157)
DROP INDEX IF EXISTS idx_black_swan_market_id;

-- user_achievements.user_id: покрыт user_achievements_pkey (user_id, achievement_id)
DROP INDEX IF EXISTS idx_user_achievements_user;

-- daily_challenges (user_id, challenge_date): покрыт idx_daily_challenges_user_date_type UNIQUE
DROP INDEX IF EXISTS idx_daily_challenges_lookup;

-- ═══════════════════════════════════════════════════════
-- 3. БЕСПОЛЕЗНЫЕ BOOLEAN ИНДЕКСЫ (низкая селективность)
-- ═══════════════════════════════════════════════════════

-- markets.is_active: boolean ~99% true, seq scan эффективнее
DROP INDEX IF EXISTS idx_markets_is_active;

-- black_swan_scenarios.is_active: покрыт partial idx_scenarios_market_active
DROP INDEX IF EXISTS idx_black_swan_is_active;

-- transactions.is_whale: покрыт partial idx_transactions_market_whale + idx_transactions_not_arb
-- Cleanup (is_whale=false) использует idx_transactions_created_at (created_at — главный предикат)
DROP INDEX IF EXISTS idx_transactions_is_whale;

-- ═══════════════════════════════════════════════════════
-- 4. ВОССТАНОВЛЕНИЕ НЕДОСТАЮЩЕГО ИНДЕКСА
-- from_address удалён миграцией 130 как "unused",
-- но smart_money (engine.py, scanner.py) и whale_analytics добавлены позже — 5 запросов.
-- ═══════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_transactions_from_address
    ON transactions(from_address)
    WHERE from_address IS NOT NULL;

-- ═══════════════════════════════════════════════════════
-- 5. FLOAT → NUMERIC (consistency с migration 179)
-- IEEE 754 float теряет точность: 0.1 + 0.2 ≠ 0.3
-- ═══════════════════════════════════════════════════════

-- timing_score: 0.0–1.0 (4 знака после запятой)
ALTER TABLE transactions
    ALTER COLUMN timing_score TYPE NUMERIC(5,4);

-- timing_score_avg: среднее timing_score, тот же диапазон
ALTER TABLE whales
    ALTER COLUMN timing_score_avg TYPE NUMERIC(5,4);

-- arb_ratio: 0.0–1.0 (доля арбитражных транзакций)
ALTER TABLE whales
    ALTER COLUMN arb_ratio TYPE NUMERIC(5,4);

-- temporal_weight: 0.0–1.0 (вес новости по свежести)
ALTER TABLE news_market_mappings
    ALTER COLUMN temporal_weight TYPE NUMERIC(4,2);

-- ═══════════════════════════════════════════════════════
-- 6. СТАНДАРТИЗАЦИЯ gen_random_uuid() (pg14+ built-in)
-- uuid_generate_v4() требует extension uuid-ossp
-- gen_random_uuid() — встроенная, быстрее, без зависимости
-- ═══════════════════════════════════════════════════════

ALTER TABLE markets ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE transactions ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE whales ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE news ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE news_market_mappings ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE users ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE consensus_votes ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE payments ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE user_subscriptions ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE daily_challenges ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE referrals ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE xp_transactions ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE market_analyses ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE user_alert_rules ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE notifications ALTER COLUMN id SET DEFAULT gen_random_uuid();
