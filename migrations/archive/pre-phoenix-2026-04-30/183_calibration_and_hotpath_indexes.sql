-- 183: Indexes для calibration query и oracle_calculator hot path

-- 1. Partial index для calibration grid search query
-- Query: WHERE outcome != 'pending' AND whale_contribution_raw IS NOT NULL
--        AND news_contribution_raw IS NOT NULL AND created_at > X
-- Покрывает calibration_repository.get_resolved_snapshots_for_calibration()
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_oracle_snapshots_calibration
    ON oracle_snapshots(created_at ASC)
    WHERE outcome != 'pending'
      AND whale_contribution_raw IS NOT NULL
      AND news_contribution_raw IS NOT NULL;

-- 2. Partial index для oracle_calculator hot path (recalculate_oracle_score)
-- Query: WHERE market_id = $1 AND is_arbitrage = false ORDER BY created_at DESC LIMIT 20
-- Вызывается при каждом whale transaction (высокая частота)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_transactions_market_nonarb
    ON transactions(market_id, created_at DESC)
    WHERE is_arbitrage = false;

-- 3. Composite index для oracle_snapshot_repository.get_summary_stats()
-- Query: WHERE outcome IN ('yes','no') AND resolved_at >= X
-- Используется в analytics API (dashboard загрузка)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_oracle_snapshots_resolved_outcome
    ON oracle_snapshots(resolved_at DESC, outcome)
    WHERE outcome IN ('yes', 'no');
