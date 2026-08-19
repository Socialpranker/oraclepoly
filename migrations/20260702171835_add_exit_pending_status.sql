-- 20260702171835_add_exit_pending_status.sql
-- Description: exit_pending — атомарный резерв намерения закрыть live-позицию
-- ДО похода на CLOB (устраняет race condition double-close, см.
-- docs/superpowers/plans/2026-07-02-audit-fixes-phase3.md продолжение).
--
-- close_live_position делал SELECT + UPDATE в 'exiting' раздельными запросами
-- без проверки rowcount — окно гонки на весь сетевой round-trip к CLOB
-- (floor-price lookup + create_and_post_market_order). Два почти одновременных
-- вызова (автотик TP + аварийная TG-кнопка) могли оба пройти SELECT-проверку
-- status='filled' и разместить два реальных SELL-ордера на бирже.
--
-- Новый шаг в жизненном цикле:
--   filled --(атомарный UPDATE...RETURNING резервирует намерение)--> exit_pending
--     --(SELL успешно отправлен на CLOB)--> exiting --(SELL matched)--> settled
--     \-(CLOB отказал/exception)-> filled (rollback, retry возможен)
--
-- 'exiting' -> 'filled' откат (CANCELLED/EXPIRED) уже существовал
-- (20260702145421_live_exit_path.sql), не меняется.

ALTER TABLE execution_orders
    DROP CONSTRAINT IF EXISTS execution_orders_status_whitelist;

ALTER TABLE execution_orders
    ADD CONSTRAINT execution_orders_status_whitelist
    CHECK (status IN (
        'intent', 'placed', 'filled', 'settled', 'rejected', 'cancelled',
        'virtual_placed', 'virtual_filled', 'virtual_settled', 'virtual_cancelled',
        'virtual_failed', 'dry_run', 'pending', 'exiting', 'exit_pending'
    ));

COMMENT ON CONSTRAINT execution_orders_status_whitelist ON execution_orders IS
    'Reject typos. Список синхронизирован с virtual_execution.py + v2_execution.py + live_exit.py.
     Добавлен exit_pending 2026-07-02 (race-condition fix для live exit path).';

-- Rollback (manual, не часть forward):
-- ALTER TABLE execution_orders DROP CONSTRAINT IF EXISTS execution_orders_status_whitelist;
-- ALTER TABLE execution_orders ADD CONSTRAINT execution_orders_status_whitelist
--   CHECK (status IN (
--     'intent', 'placed', 'filled', 'settled', 'rejected', 'cancelled',
--     'virtual_placed', 'virtual_filled', 'virtual_settled', 'virtual_cancelled',
--     'virtual_failed', 'dry_run', 'pending', 'exiting'
--   ));
