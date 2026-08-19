-- 20260320135209_drop_phase5_tables.sql
-- Phase 5: Drop simulation/whale/cwi/paper tables (dashboard v2 refactoring)

DROP TABLE IF EXISTS simulation_daily_snapshots CASCADE;
DROP TABLE IF EXISTS whale_segment_snapshots CASCADE;
DROP TABLE IF EXISTS cwi_paper_positions CASCADE;
DROP TABLE IF EXISTS cwi_bank_state CASCADE;
DROP TABLE IF EXISTS cwi_bank_ledger CASCADE;
DROP TABLE IF EXISTS model_predictions CASCADE;
DROP TABLE IF EXISTS model_registry CASCADE;
