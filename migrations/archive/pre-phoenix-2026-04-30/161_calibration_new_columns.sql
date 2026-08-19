-- 161_calibration_new_columns.sql
-- Добавляет недостающие колонки для grid search (w_fresh, w_cluster, etc.)
-- и beta calibration параметры. Фиксит pre-existing баг: grid search
-- не может сохранить конфиг без этих колонок.

ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS w_fresh NUMERIC(6,4) DEFAULT 0.6;
ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS w_cluster NUMERIC(6,4) DEFAULT 0.7;
ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS w_registry NUMERIC(6,4) DEFAULT 0.3;
ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS w_oi NUMERIC(6,4) DEFAULT 0.4;
ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS w_concentration NUMERIC(6,4) DEFAULT 0.3;
ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS w_micro NUMERIC(6,4) DEFAULT 0.35;
ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS beta_a NUMERIC(6,4) DEFAULT 1.0;
ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS beta_b NUMERIC(6,4) DEFAULT 0.0;
ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS calibration_method TEXT DEFAULT 'temperature';
