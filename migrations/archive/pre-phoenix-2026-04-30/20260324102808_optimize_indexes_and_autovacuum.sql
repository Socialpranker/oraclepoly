-- 20260324102808_optimize_indexes_and_autovacuum.sql
-- Оптимизация индексов: whale lookups + oracle_snapshots DISTINCT ON
--
-- Проблемы:
--   1. LOWER(address) в 7 местах кода убивает unique index на whales.address (93K вызовов/9 дней)
--   2. DISTINCT ON (market_id) по oracle_snapshots сканирует 470K-592K tuples/scan
--   3. 3 partial индекса на resolved строках дублируют друг друга, ни один не оптимален
--   4. Autovacuum порог 500K dead tuples — visibility map хронически устаревшая

-- 1. Expression index для LOWER(address) — ускорит все whale lookups в 20-50x
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_whales_address_lower
    ON whales (LOWER(address));

-- 2. Composite covering index для DISTINCT ON (market_id) в resolved CTE
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_oracle_snapshots_market_resolved
    ON oracle_snapshots (market_id, resolved_at DESC)
    INCLUDE (outcome)
    WHERE outcome IN ('yes', 'no');

-- 3. DROP неэффективных индексов:
-- idx_oracle_snapshots_outcome: 485K scans, 55B tuples, ключ (outcome) бесполезен для DISTINCT ON
DROP INDEX CONCURRENTLY IF EXISTS idx_oracle_snapshots_outcome;
-- idx_oracle_snapshots_resolved_at_analytics: 120K scans, 71B tuples, дубль
DROP INDEX CONCURRENTLY IF EXISTS idx_oracle_snapshots_resolved_at_analytics;
-- idx_oracle_snapshots_resolved_outcome: заменён на market_resolved выше
DROP INDEX CONCURRENTLY IF EXISTS idx_oracle_snapshots_resolved_outcome;

-- 4. Autovacuum tuning для oracle_snapshots (2.5M строк)
-- Default порог 500K dead tuples, снижаем до ~55K
ALTER TABLE oracle_snapshots SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_threshold = 5000,
    autovacuum_analyze_scale_factor = 0.05
);
