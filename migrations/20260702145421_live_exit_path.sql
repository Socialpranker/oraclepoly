-- 20260702145421_live_exit_path.sql
-- Description: live exit path (audit топ-1) — трекинг SELL-ордера выхода из live-позиции.
--
-- Live-путь был BUY-only: SELL не существовал нигде, TP/SL — только virtual,
-- лузер ехал до -100% стейка. См. research/design-audit-2026-07-02.md.
--
-- 'exiting' — новый статус, шаг между 'filled' и 'settled' (outcome='EARLY_EXIT'):
--   filled --(close_live_position: SELL placed)--> exiting --(SELL matched)--> settled
--                                                          \-(SELL cancelled/expired)-> filled (retry)

ALTER TABLE public.execution_orders
    ADD COLUMN IF NOT EXISTS exit_order_id text,
    ADD COLUMN IF NOT EXISTS exit_requested_at timestamptz;

COMMENT ON COLUMN execution_orders.exit_order_id IS
    'CLOB orderID SELL-ордера live exit (close_live_position). NULL пока позиция не в exiting.';
COMMENT ON COLUMN execution_orders.exit_requested_at IS
    'Момент постановки SELL exit-ордера (close_live_position). NULL пока позиция не в exiting.';

-- Status whitelist CHECK constraint (migration 20260520131136_execution_orders_hard_constraints.sql)
-- не содержит 'exiting' — расширяем список тем же приёмом DROP+ADD, что и
-- 20260524184802_raise_stake_max_to_1000.sql. Полный актуальный список сверен по
-- 20260520131136 (13 статусов) + 'exiting' (14-й, новый).
ALTER TABLE execution_orders
    DROP CONSTRAINT IF EXISTS execution_orders_status_whitelist;

ALTER TABLE execution_orders
    ADD CONSTRAINT execution_orders_status_whitelist
    CHECK (status IN (
        'intent', 'placed', 'filled', 'settled', 'rejected', 'cancelled',
        'virtual_placed', 'virtual_filled', 'virtual_settled', 'virtual_cancelled',
        'virtual_failed', 'dry_run', 'pending', 'exiting'
    ));

COMMENT ON CONSTRAINT execution_orders_status_whitelist ON execution_orders IS
    'Reject typos. Список синхронизирован с virtual_execution.py + v2_execution.py + live_exit.py.
     Добавлен exiting 2026-07-02 (live exit path, audit топ-1).';

-- Rollback (manual, не часть forward):
-- ALTER TABLE execution_orders DROP CONSTRAINT IF EXISTS execution_orders_status_whitelist;
-- ALTER TABLE execution_orders ADD CONSTRAINT execution_orders_status_whitelist
--   CHECK (status IN (
--     'intent', 'placed', 'filled', 'settled', 'rejected', 'cancelled',
--     'virtual_placed', 'virtual_filled', 'virtual_settled', 'virtual_cancelled',
--     'virtual_failed', 'dry_run', 'pending'
--   ));
-- ALTER TABLE execution_orders DROP COLUMN IF EXISTS exit_order_id;
-- ALTER TABLE execution_orders DROP COLUMN IF EXISTS exit_requested_at;
