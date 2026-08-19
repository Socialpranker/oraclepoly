-- 20260408121553_calibration_new_factors.sql
-- Новые raw factors в oracle_snapshots: retail + volatility.
-- Calibrated 2026-04-08 на 278K resolved markets (walk-forward grid search).

ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS retail_factor_raw NUMERIC(10, 6) DEFAULT 0;
ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS volatility_factor_raw NUMERIC(10, 6) DEFAULT 0;
