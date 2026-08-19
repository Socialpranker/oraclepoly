-- Add BSS and Murphy decomposition columns to daily_benchmarks
ALTER TABLE daily_benchmarks ADD COLUMN IF NOT EXISTS bss FLOAT;
ALTER TABLE daily_benchmarks ADD COLUMN IF NOT EXISTS bss_7d FLOAT;
ALTER TABLE daily_benchmarks ADD COLUMN IF NOT EXISTS bss_14d FLOAT;
ALTER TABLE daily_benchmarks ADD COLUMN IF NOT EXISTS bss_30d FLOAT;
ALTER TABLE daily_benchmarks ADD COLUMN IF NOT EXISTS murphy_reliability FLOAT;
ALTER TABLE daily_benchmarks ADD COLUMN IF NOT EXISTS murphy_resolution FLOAT;
