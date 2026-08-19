-- 20260324092844_add_w_whale_contra.sql
-- Add whale contra-indicator interaction weight to calibration_configs

ALTER TABLE calibration_configs ADD COLUMN IF NOT EXISTS w_whale_contra NUMERIC DEFAULT 0;
