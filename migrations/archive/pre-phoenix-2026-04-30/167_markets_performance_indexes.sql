-- Migration 167: Составные индексы для производительности списков
-- Проблема: SELECT * FROM markets WHERE is_active ORDER BY volume_24h DESC — full table scan

-- Основной индекс для листинга рынков (покрывает WHERE + ORDER BY)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_markets_active_volume
    ON markets (is_active, volume_24h DESC);

-- Индекс для фильтрации по категории + сортировка
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_markets_active_category_volume
    ON markets (is_active, category, volume_24h DESC);

-- Аналогичный для events (уже есть отдельные idx_events_active и idx_events_volume, но не составной)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_active_volume
    ON events (is_active, volume_24h DESC);

-- Индекс для transactions ORDER BY created_at DESC с LEFT JOIN
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_transactions_created_desc_covering
    ON transactions (created_at DESC)
    INCLUDE (id, direction, amount, is_whale, market_id, market_title, market_slug, outcome, price, whale_id);
