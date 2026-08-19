-- PR1: stake-aware fill probability в shadow mode.
-- Поля заполняются при virtual placement; используются для paper vs expected-live
-- divergence dashboard. Не меняют поведение settlement.

ALTER TABLE execution_orders
  ADD COLUMN IF NOT EXISTS expected_fill_rate    numeric(4, 3),
  ADD COLUMN IF NOT EXISTS book_depth_usd        numeric(12, 2),
  ADD COLUMN IF NOT EXISTS book_top_size         numeric(10, 2),
  ADD COLUMN IF NOT EXISTS fill_probability_reason text;

COMMENT ON COLUMN execution_orders.expected_fill_rate IS
  'Доля stake которая filled бы при текущем live book (0.0-1.0). Заполняется при virtual placement.';
COMMENT ON COLUMN execution_orders.book_depth_usd IS
  'Суммарный USDC на levels at_or_better limit_price (для side buy).';
COMMENT ON COLUMN execution_orders.book_top_size IS
  'Size on best opposing level (для side buy = best ask size).';

-- Индекс для analytics queries
CREATE INDEX IF NOT EXISTS idx_execution_orders_expected_fill_rate
  ON execution_orders (expected_fill_rate)
  WHERE expected_fill_rate IS NOT NULL;
