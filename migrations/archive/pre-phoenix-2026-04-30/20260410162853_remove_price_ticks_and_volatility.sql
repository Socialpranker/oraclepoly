-- 20260410162853_remove_price_ticks_and_volatility.sql
-- Полное удаление price_ticks feature и volatility scoring subsystem.
--
-- Context:
-- На проде 2026-04-10 обнаружено: таблица price_ticks пуста (0 rows),
-- WebSocket flush loop не работает (ни одного ws_price_ticks_flushed за 48h),
-- cron price-ticks-sync не зарегистрирован в crontab.
-- volatility_factor_raw = 0 для всех 481K oracle_snapshots за последние 48h.
-- volatility_contribution = w_volatility * 0 = 0 всегда → фактор dead.
--
-- Решение: убрать feature целиком (DB + backend + frontend + calibration).
-- Migration file + код изменения в одном коммите Phase C.
--
-- Affected tables:
-- 1. DROP TABLE price_ticks CASCADE (все индексы/триггеры/FK)
-- 2. ALTER TABLE oracle_snapshots DROP COLUMN volatility_factor_raw
-- 3. ALTER TABLE calibration_configs DROP COLUMN w_volatility (если существует)

DROP TABLE IF EXISTS price_ticks CASCADE;

ALTER TABLE oracle_snapshots DROP COLUMN IF EXISTS volatility_factor_raw;
ALTER TABLE oracle_snapshots DROP COLUMN IF EXISTS volatility_contribution;

ALTER TABLE calibration_configs DROP COLUMN IF EXISTS w_volatility;
