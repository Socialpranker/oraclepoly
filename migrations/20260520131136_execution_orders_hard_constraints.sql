-- 20260520131136_execution_orders_hard_constraints.sql
-- Description: Phase 1 Stage 6 — DB hard constraints для execution_orders.
--
-- Layer 3 defence: даже если application-layer (safety/risk_engine) пропустит
-- bad write — DB constraint его не примет. См. документ §9 strategy_10k_capital.
--
-- Prod audit (2026-05-20): 8007 rows clean
--   - max stake $400 (<$500 cap)
--   - 4 distinct statuses all in whitelist (rejected/virtual_cancelled/
--     virtual_filled/virtual_settled)
--   - sides только YES/NO
--   - 0 bad fill_price / signal_price (bounds (0, 1))
-- → все 4 constraints применяются без cleanup.
--
-- Idempotency UNIQUE constraint skipped — уже существуют:
--   * uq_execution_orders_signal (detector_signal_id) — один intent per signal
--   * uq_execution_orders_open_market (detector, market_id, side) WHERE status
--     IN open — одна открытая позиция per market+side
--
-- См. docs/superpowers/plans/2026-05-risk-engine-hardening/06-db-constraints.md.

-- 1. Stake hard cap: $500 = 5% × $10k bankroll (mirror Stage 3).
-- Application layer уже enforce'ит min_stake $25 + per_bet_cap 5%. Этот CHECK —
-- последний барьер от bug в коде (typo в config, missed migration, race condition).
ALTER TABLE execution_orders
  ADD CONSTRAINT execution_orders_stake_max
  CHECK (stake IS NULL OR stake <= 500.0);

-- 2. Status whitelist. Reject любой status не в списке —
-- prevent typo в коде типа status='filed' вместо 'filled'.
ALTER TABLE execution_orders
  ADD CONSTRAINT execution_orders_status_whitelist
  CHECK (status IN (
    'intent', 'placed', 'filled', 'settled', 'rejected', 'cancelled',
    'virtual_placed', 'virtual_filled', 'virtual_settled', 'virtual_cancelled',
    'virtual_failed', 'dry_run', 'pending'
  ));

-- 3. Side whitelist — только YES/NO. Polymarket binary markets.
ALTER TABLE execution_orders
  ADD CONSTRAINT execution_orders_side_whitelist
  CHECK (side IN ('YES', 'NO'));

-- 4. Price bounds: fill_price и signal_price в открытом интервале (0, 1).
-- 0 или 1 означали бы resolved market — это не должно попадать в execution_orders.
ALTER TABLE execution_orders
  ADD CONSTRAINT execution_orders_price_bounds
  CHECK (
    (fill_price IS NULL OR (fill_price > 0 AND fill_price < 1)) AND
    (signal_price IS NULL OR (signal_price > 0 AND signal_price < 1))
  );

COMMENT ON CONSTRAINT execution_orders_stake_max ON execution_orders IS
  'Hard cap: per-trade stake <= $500. Mirror Stage 3 per_bet_cap (5% × $10k).';
COMMENT ON CONSTRAINT execution_orders_status_whitelist ON execution_orders IS
  'Reject typos. Список синхронизирован с virtual_execution.py + v2_execution.py.';
COMMENT ON CONSTRAINT execution_orders_side_whitelist ON execution_orders IS
  'Polymarket binary markets: только YES/NO.';
COMMENT ON CONSTRAINT execution_orders_price_bounds ON execution_orders IS
  'fill_price/signal_price ∈ (0, 1). 0 или 1 = resolved market — не должно попадать в open orders.';

-- Rollback (manual, не часть forward):
-- ALTER TABLE execution_orders DROP CONSTRAINT IF EXISTS execution_orders_stake_max;
-- ALTER TABLE execution_orders DROP CONSTRAINT IF EXISTS execution_orders_status_whitelist;
-- ALTER TABLE execution_orders DROP CONSTRAINT IF EXISTS execution_orders_side_whitelist;
-- ALTER TABLE execution_orders DROP CONSTRAINT IF EXISTS execution_orders_price_bounds;
