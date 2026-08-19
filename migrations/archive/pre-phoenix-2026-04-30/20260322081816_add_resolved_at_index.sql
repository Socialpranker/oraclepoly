-- 20260322081816_add_resolved_at_index.sql
-- Ускорение аналитических запросов с фильтром по resolved_at.
-- Текущий idx_oracle_snapshots_outcome (outcome WHERE != 'pending') сканирует все 536K+ resolved строк,
-- затем фильтрует resolved_at post-scan. Новый индекс позволяет range scan по resolved_at.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_oracle_snapshots_resolved_at_analytics
    ON oracle_snapshots (resolved_at DESC)
    WHERE outcome <> 'pending';
