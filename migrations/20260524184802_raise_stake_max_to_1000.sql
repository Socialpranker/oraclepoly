-- 20260524184802_raise_stake_max_to_1000.sql
-- Description: Поднять execution_orders.stake hard cap с $500 до $1000.
--
-- Контекст:
--   2026-05-24: portfolio 'rules' bankroll увеличен $8K → $15K + Kelly=0.05
--   MAX_STAKE_PCT_DEFAULT = 0.05 в ev_sizer.py
--   $15,000 × 0.05 = $750 max stake → нарушает старый cap $500
--
-- Original constraint (migration 20260520131136):
--   Hard cap mirror Stage 3 per_bet_cap (5% × $10k = $500)
--
-- Новое значение:
--   Hard cap = 5% × $20k headroom = $1000
--   Дает запас на bankroll growth до $20K без code changes
--
-- Safety: остальные Layer 1 (sizer) + Layer 2 (risk_engine) защиты не меняются.
-- DB constraint остаётся последним барьером — просто с более просторным cap.

ALTER TABLE execution_orders
  DROP CONSTRAINT IF EXISTS execution_orders_stake_max;

ALTER TABLE execution_orders
  ADD CONSTRAINT execution_orders_stake_max
  CHECK (stake IS NULL OR stake <= 1000.0);

COMMENT ON CONSTRAINT execution_orders_stake_max ON execution_orders IS
  'Hard cap: per-trade stake <= $1000. Raised 2026-05-24 from $500 to support $15K bankroll @ 5% per-bet (was $10K × 5% = $500).';

-- Rollback (manual):
-- ALTER TABLE execution_orders DROP CONSTRAINT IF EXISTS execution_orders_stake_max;
-- ALTER TABLE execution_orders ADD CONSTRAINT execution_orders_stake_max
--   CHECK (stake IS NULL OR stake <= 500.0);
