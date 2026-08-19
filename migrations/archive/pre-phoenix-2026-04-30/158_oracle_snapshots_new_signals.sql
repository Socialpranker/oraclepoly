-- 158: Добавить raw contributions новых сигналов в oracle_snapshots
-- Необходимо для grid search калибровки весов w_oi, w_concentration, w_micro, smart money

ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS oi_factor_raw NUMERIC(10,6);
ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS concentration_factor_raw NUMERIC(10,6);
ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS micro_factor_raw NUMERIC(10,6);
ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS sm_fresh_raw NUMERIC(10,6);
ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS sm_cluster_raw NUMERIC(10,6);
ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS sm_registry_raw NUMERIC(10,6);
