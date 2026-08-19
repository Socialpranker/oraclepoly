-- 20260702153645_basket_execution.sql
-- Description: multi-leg basket координатор (audit research-gap) — parent_basket_id
-- в execution_orders. Каждая нога D29/D40 basket-арбитража пишется отдельной строкой
-- execution_orders; общий parent_basket_id связывает ноги одной корзины. Статус
-- корзины — вычислимый из статусов ног (без отдельной таблицы, см.
-- app/services/basket_execution.py).

ALTER TABLE public.execution_orders
    ADD COLUMN IF NOT EXISTS parent_basket_id uuid;

CREATE INDEX IF NOT EXISTS idx_execution_orders_parent_basket_id
    ON execution_orders (parent_basket_id)
    WHERE parent_basket_id IS NOT NULL;

COMMENT ON COLUMN execution_orders.parent_basket_id IS
    'Multi-leg basket coordinator (D29/D40, audit research-gap): общий id для всех ног
     одной basket-корзины (buy NO на все рынки события). NULL для обычных single-leg
     ордеров. Статус корзины вычисляется агрегатом по статусам строк с одним
     parent_basket_id — см. app/services/basket_execution.py.';

-- Rollback (manual, не часть forward):
-- DROP INDEX IF EXISTS idx_execution_orders_parent_basket_id;
-- ALTER TABLE execution_orders DROP COLUMN IF EXISTS parent_basket_id;
