-- Add early-exit (Take-Profit / Stop-Loss) columns to execution_orders.
--
-- Rationale (2026-05-13): добавляем active position management поверх
-- buy-and-hold-to-resolution model. Position manager job будет закрывать
-- открытые virtual_filled позиции когда current_price пересекает TP/SL пороги.
--
-- Колонки:
--   exit_reason          — 'take_profit' | 'stop_loss' | 'manual_close' | NULL
--   exit_price           — current_price на момент TP/SL trigger
--   exit_at              — timestamp early exit (раньше settled_at)
--
-- При early exit:
--   status              = 'virtual_settled'
--   outcome             = 'EARLY_EXIT'  (новое значение, не WIN/LOSS)
--   pnl                 = stake × (exit_price/fill_price − 1)  для YES
--                       = stake × ((1−exit_price)/(1−fill_price) − 1)  для NO
--   trading_fee_usd     = 2 × estimate_trading_fee(...)  (entry + exit)
--   slippage_usd        = stake × half_spread × 2   (round-trip)
--   net_pnl             = pnl − fee − slippage

ALTER TABLE execution_orders
    ADD COLUMN IF NOT EXISTS exit_reason TEXT,
    ADD COLUMN IF NOT EXISTS exit_price NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS exit_at TIMESTAMP WITH TIME ZONE;

CREATE INDEX IF NOT EXISTS idx_execution_orders_exit_reason
    ON execution_orders (exit_reason)
    WHERE exit_reason IS NOT NULL;

COMMENT ON COLUMN execution_orders.exit_reason IS
    'NULL = held to resolution; take_profit / stop_loss / manual_close = early-exit by position_manager';
COMMENT ON COLUMN execution_orders.exit_price IS
    'current_price at TP/SL trigger time';
COMMENT ON COLUMN execution_orders.exit_at IS
    'when position was closed early (before market resolution)';
