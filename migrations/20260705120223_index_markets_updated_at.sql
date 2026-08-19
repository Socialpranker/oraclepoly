-- 20260705120223_index_markets_updated_at.sql
-- markets.updated_at пишется каждые 15 мин (market_sync) + каждые 30с
-- (WS write-through) на ~89K строк, читается freshness-запросами
-- (MAX(updated_at), data_audit) — до сих пор был full scan.
-- Без CONCURRENTLY: run_migration.py применяет в транзакции; 89K строк — секунды.
CREATE INDEX IF NOT EXISTS idx_markets_updated_at ON markets (updated_at);
