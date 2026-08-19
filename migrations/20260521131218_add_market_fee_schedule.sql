-- 20260521131218_add_market_fee_schedule.sql
-- Description: Per-market fee schedule из Gamma API.
--
-- Сейчас fee_calculator.py использует hardcoded таблицу per-category
-- (politics 4%, sports 3%, crypto 7.2% и т.д.). Polymarket Gamma отдаёт
-- честный feeSchedule на каждый market: {exponent, rate, takerOnly, rebateRate}.
-- Это позволяет:
--   1. Считать fees без расхождений с реальностью (категория ≠ fee bucket иногда).
--   2. Учитывать maker rebate (rebateRate 0.25 = 25% возвращается maker).
--   3. Самообновляться когда Polymarket меняет fees (например, launch incentive).
--
-- Источник: research/gamma-api-2026-05/unique_fields.md (top-5 по value/effort).

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS fee_schedule JSONB;

COMMENT ON COLUMN markets.fee_schedule IS
    'Per-market fee schedule from Gamma feeSchedule: {exponent, rate, takerOnly, rebateRate}. Fallback на hardcoded категорийную таблицу если NULL.';

-- Rollback:
-- ALTER TABLE markets DROP COLUMN IF EXISTS fee_schedule;
