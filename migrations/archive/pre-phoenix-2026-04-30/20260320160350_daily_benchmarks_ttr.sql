-- 20260320160350_daily_benchmarks_ttr.sql
-- Description: Добавить колонки Time to Resolve (TTR) в daily_benchmarks

ALTER TABLE daily_benchmarks ADD COLUMN IF NOT EXISTS avg_ttr_hours NUMERIC(10, 2);
ALTER TABLE daily_benchmarks ADD COLUMN IF NOT EXISTS median_ttr_hours NUMERIC(10, 2);
ALTER TABLE daily_benchmarks ADD COLUMN IF NOT EXISTS p75_ttr_hours NUMERIC(10, 2);
