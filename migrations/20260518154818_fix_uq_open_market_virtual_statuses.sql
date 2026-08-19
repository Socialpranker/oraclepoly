-- 20260518154818_fix_uq_open_market_virtual_statuses.sql
-- Description: Fix uq_execution_orders_open_market to also cover virtual statuses.
--
-- Bug (discovered 2026-05-18):
--   Original index from 20260501120002_execution_orders_v2.sql had
--   WHERE status IN ('intent', 'placed', 'filled') — only live statuses.
--   But virtual mode uses 'virtual_placed' / 'virtual_filled', so the index
--   never fired for paper trading. Result: detectors that tick on cron
--   would open N duplicate positions on the same (detector, market, side)
--   while the previous one was still open, inflating virtual PnL N×.
--
--   Concrete example: STANDALONE_V2_LGB на market 0x20ac4f5f... — 31 orders,
--   все fill=0.5000 / exit=0.0005, settled-paper PnL +$4242 вместо ~+$140.
--
-- Fix: DROP старый индекс, CREATE заново с добавленными virtual статусами.
-- ВАЖНО: одна и та же запись не должна одновременно сидеть в обоих наборах,
-- и историческое состояние (где дубликаты уже есть как 'virtual_settled')
-- индекс не трогает — он фильтрует только open-состояния.

DROP INDEX IF EXISTS public.uq_execution_orders_open_market;

CREATE UNIQUE INDEX uq_execution_orders_open_market
    ON public.execution_orders (detector, market_id, side)
    WHERE status IN ('intent', 'placed', 'filled', 'virtual_placed', 'virtual_filled');
