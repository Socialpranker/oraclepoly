-- 182: FK constraints, composite indexes, missing indexes
-- Fixes: notifications orphaning, alert_matcher seq scan, experiments no indexes

-- 1. FK на notifications.user_id → users(id)
-- Сначала удалим orphan записи (user_id ссылается на несуществующего user)
DELETE FROM notifications
WHERE user_id NOT IN (SELECT id FROM users);

ALTER TABLE notifications
    ADD CONSTRAINT fk_notifications_user
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;

-- 2. Composite index для alert_matcher.py:83 hot path
-- Query: WHERE rule_type = $1 AND is_active = true AND (market_id = $2 OR market_id IS NULL)
-- Заменяет 3 отдельных partial index (rule_type, market_id, user_id WHERE is_active)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_alert_rules_type_active_market
    ON user_alert_rules(rule_type, market_id)
    WHERE is_active = true;

-- 3. Experiments — ноль индексов кроме PK. Добавить status + started_at
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_experiments_status
    ON experiments(status, started_at DESC);

-- 4. experiment_snapshots — частый lookup по experiment_id + model_key
-- UNIQUE constraint уже есть, но нет отдельного index для range scans по date
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_experiment_snapshots_date_range
    ON experiment_snapshots(experiment_id, model_key, snapshot_date DESC);
