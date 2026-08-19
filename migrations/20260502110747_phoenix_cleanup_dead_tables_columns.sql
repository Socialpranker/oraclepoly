-- 20260502110747_phoenix_cleanup_dead_tables_columns.sql
-- Description: Cleanup post-Phoenix V2 / Pipeline V2.
--   1. Drop 6 dead таблиц (нет reads/writes из app/ scripts/ tests/).
--   2. Drop 2 dead колонки execution_orders.{oracle_score, divergence}
--      — V2 path использует calibrated_p + ev_pct, эти колонки писал только
--      legacy execution_repository.insert_order() (модуль удалён в этом PR).
--
-- ВНИМАНИЕ: Перед применением выполнить pg_dump на VPS:
--   docker exec oraclepoly-postgres pg_dump -U oraclepoly -d oraclepoly \
--     -t smart_money_wallets -t wallet_clusters -t news_market_mappings \
--     -t daily_benchmarks -t funding_chains -t event_arbitrage_scans \
--     -t execution_orders \
--     | gzip > /root/backups/phoenix_cleanup_$(date +%Y%m%d).sql.gz

-- Drop 6 dead tables
DROP TABLE IF EXISTS event_arbitrage_scans CASCADE;
DROP TABLE IF EXISTS funding_chains CASCADE;
DROP TABLE IF EXISTS daily_benchmarks CASCADE;
DROP TABLE IF EXISTS news_market_mappings CASCADE;
DROP TABLE IF EXISTS wallet_clusters CASCADE;
DROP TABLE IF EXISTS smart_money_wallets CASCADE;

-- Drop 2 dead columns from execution_orders (V2 не пишет, legacy insert_order удалён)
ALTER TABLE execution_orders DROP COLUMN IF EXISTS oracle_score;
ALTER TABLE execution_orders DROP COLUMN IF EXISTS divergence;
