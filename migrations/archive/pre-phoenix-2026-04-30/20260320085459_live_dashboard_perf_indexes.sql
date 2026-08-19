-- 20260320085459_live_dashboard_perf_indexes.sql
-- Индексы для ускорения Live Trading dashboard

-- 1. Композитный индекс для DISTINCT ON (model_id, market_id) ORDER BY computed_at DESC
-- Ускоряет get_all_latest_predictions() — subquery теперь использует index-only scan
CREATE INDEX IF NOT EXISTS idx_model_pred_model_market_computed
    ON model_predictions(model_id, market_id, computed_at DESC);

-- 2. Partial index для get_live_signals_data():
-- DISTINCT ON (market_id) WHERE outcome = 'pending' ORDER BY market_id, created_at DESC
-- Существующий idx_oracle_snapshots_outcome имеет WHERE outcome != 'pending' — исключает pending!
-- Без этого индекса: full table scan + heap filter по outcome
CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_pending_market
    ON oracle_snapshots(market_id, created_at DESC)
    WHERE outcome = 'pending';
