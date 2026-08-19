-- 20260705100732_fix_redeem_pending_index_execution_mode.sql
-- Description: фикс partial index idx_execution_orders_redeem_pending — предыдущая
-- миграция (20260705093609) фильтровала по execution_mode = 'live', но такого
-- значения в колонке нет: реальные live-ордера имеют execution_mode IN ('maker',
-- 'taker') (virtual-позиции — 'virtual'). redeem_job.py._FETCH_CANDIDATES_SQL
-- исправлен на тот же фильтр — индекс пересоздаём, чтобы он реально покрывал запрос.

DROP INDEX IF EXISTS idx_execution_orders_redeem_pending;

CREATE INDEX IF NOT EXISTS idx_execution_orders_redeem_pending
    ON execution_orders (execution_mode, status, outcome)
    WHERE execution_mode IN ('maker', 'taker') AND status = 'settled' AND outcome = 'WIN'
      AND redeem_status IS DISTINCT FROM 'redeemed';

-- Rollback (manual, не часть forward):
-- DROP INDEX IF EXISTS idx_execution_orders_redeem_pending;
-- CREATE INDEX idx_execution_orders_redeem_pending
--     ON execution_orders (execution_mode, status, outcome)
--     WHERE execution_mode = 'live' AND status = 'settled' AND outcome = 'WIN'
--       AND redeem_status IS DISTINCT FROM 'redeemed';

