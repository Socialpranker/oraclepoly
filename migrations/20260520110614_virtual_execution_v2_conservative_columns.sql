-- 20260520110614_virtual_execution_v2_conservative_columns.sql
-- Description: Phase 0 Stage 2 — добавить колонки для conservative virtual model.
--
-- Conservative model заменяет fill-on-touch на strict price-through (best_bid > limit),
-- добавляет adverse selection penalty (P_informed × half_spread per fill), tracks
-- queue position at placement для maker GTC orders.
--
-- simulation_model_version разделяет старые оптимистичные записи (v1_optimistic) от
-- новых честных (v2_conservative). Метрики и audit-queries используют only v2 данные.
--
-- См. docs/superpowers/plans/2026-05-honest-baseline/02-conservative-queue.md.

ALTER TABLE execution_orders
  ADD COLUMN IF NOT EXISTS simulation_model_version text DEFAULT 'v1_optimistic',
  ADD COLUMN IF NOT EXISTS queue_position_at_placement numeric(14, 4),
  ADD COLUMN IF NOT EXISTS as_penalty_usd numeric(12, 6),
  ADD COLUMN IF NOT EXISTS effective_fill_price numeric(10, 6);

COMMENT ON COLUMN execution_orders.simulation_model_version IS
  'v1_optimistic (fill on touch, no AS) | v2_conservative (strict price-through + queue + AS penalty). См. Phase 0 Stage 2.';

COMMENT ON COLUMN execution_orders.queue_position_at_placement IS
  'Maker GTC: размер queue (USD volume) на нашем limit-level в момент placement. NULL для taker или v1.';

COMMENT ON COLUMN execution_orders.as_penalty_usd IS
  'Adverse selection cost в USD = stake × P_informed × half_spread / fill_price. NULL для v1 или unfilled.';

COMMENT ON COLUMN execution_orders.effective_fill_price IS
  'fill_price + AS penalty per share. Используется для honest realized_roi_net расчёта.';

CREATE INDEX IF NOT EXISTS idx_execution_orders_sim_model
  ON execution_orders(simulation_model_version)
  WHERE simulation_model_version IS NOT NULL;
