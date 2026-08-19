-- DB Optimization: Phase 2 (DROP индексов, -212 MB) + Phase 6 (DROP мёртвых таблиц, -12 MB)
--
-- Phase 2: Индексы с минимальным использованием (< 50 scans) и высоким размером
-- Phase 6: Таблицы с 0 строк, не используемые ни одним модулем (verified grep)

-- === Phase 2: DROP неэффективных индексов (-212 MB) ===

-- 131 MB, 43 scans — БОЛЬШЕ самой таблицы transactions (93 MB)
DROP INDEX CONCURRENTLY IF EXISTS idx_transactions_created_desc_covering;

-- 64 MB, 37 scans — поглощён idx_oracle_snapshots_market_created и другими
DROP INDEX CONCURRENTLY IF EXISTS idx_oracle_snapshots_created;

-- 7.6 MB, 10 scans — resolved_at редко используется в WHERE
DROP INDEX CONCURRENTLY IF EXISTS idx_oracle_snapshots_resolved_at;

-- 4 MB, 5 scans — is_taker NULL фильтр устарел
DROP INDEX CONCURRENTLY IF EXISTS idx_transactions_is_taker_null;

-- 2.4 MB, 0 scans — ebq_score не используется в запросах
DROP INDEX CONCURRENTLY IF EXISTS idx_whales_ebq_score;

-- 2.3 MB, 0 scans — signal_strength не используется в запросах
DROP INDEX CONCURRENTLY IF EXISTS idx_whales_signal_strength;

-- 0.5 MB, 0 scans — wins_losses composite не используется
DROP INDEX CONCURRENTLY IF EXISTS idx_whales_wins_losses;

-- === Phase 6: DROP мёртвых таблиц (-12 MB) ===
-- Все таблицы: 0 строк, 0 scans, не referenced в коде (grep verified)

DROP TABLE IF EXISTS orderbook_snapshots CASCADE;
DROP TABLE IF EXISTS live_paper_positions CASCADE;
DROP TABLE IF EXISTS wallet_pnl CASCADE;
DROP TABLE IF EXISTS sampling_markets CASCADE;
DROP TABLE IF EXISTS crowd_wisdom_snapshots CASCADE;
DROP TABLE IF EXISTS user_watchlist CASCADE;
DROP TABLE IF EXISTS referrals CASCADE;
