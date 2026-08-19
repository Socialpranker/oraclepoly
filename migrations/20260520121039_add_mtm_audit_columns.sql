-- 20260520121039_add_mtm_audit_columns.sql
-- Description: Phase 1 Stage 1 — MtM tracking columns в execution_orders.
--
-- Periodic cron job (mtm-refresh) обновляет mtm_price_last_check и
-- unrealized_pnl_last для open positions (status placed/filled, settled_at IS NULL).
-- check_daily_pnl_limit_mtm читает unrealized_pnl_last в сумме для preventive
-- daily loss kill (vs reactive только realized).
--
-- Используем existing fill_price колонку как entry_price (она уже side-adjusted —
-- см. SSoT app/jobs/position_manager.py:151-153). Не дублируем entry_price_at_placement.
--
-- См. docs/superpowers/plans/2026-05-risk-engine-hardening/01-mtm-daily-pnl.md.

ALTER TABLE execution_orders
  ADD COLUMN IF NOT EXISTS mtm_price_last_check numeric(10, 6),
  ADD COLUMN IF NOT EXISTS mtm_updated_at timestamptz,
  ADD COLUMN IF NOT EXISTS unrealized_pnl_last numeric(12, 4);

COMMENT ON COLUMN execution_orders.mtm_price_last_check IS
  'Last canonical YES price (markets.current_price) использованная для MtM расчёта. Обновляется cron mtm-refresh каждую минуту.';

COMMENT ON COLUMN execution_orders.mtm_updated_at IS
  'Timestamp последнего MtM recalc. NULL/stale (>5min) → MtM-aware daily PnL ignore эту позицию (conservative).';

COMMENT ON COLUMN execution_orders.unrealized_pnl_last IS
  'Unrealized PnL (equity − stake) USD для open position. Считается из bankroll_mtm.compute_bankroll_with_mtm.';

CREATE INDEX IF NOT EXISTS idx_execution_orders_mtm_open
  ON execution_orders(mtm_updated_at)
  WHERE settled_at IS NULL AND status IN ('placed', 'filled', 'virtual_placed', 'virtual_filled');
