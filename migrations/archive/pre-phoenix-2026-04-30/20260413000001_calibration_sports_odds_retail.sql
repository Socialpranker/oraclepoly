-- migrations/20260413000001_calibration_sports_odds_retail.sql
-- Добавить w_sports_odds и w_retail в calibration_configs.
-- Без этих колонок calibration и calibration-decorrelation jobs падают.

ALTER TABLE calibration_configs
    ADD COLUMN IF NOT EXISTS w_sports_odds NUMERIC(6,4) DEFAULT 0.50,
    ADD COLUMN IF NOT EXISTS w_retail      NUMERIC(6,4) DEFAULT -0.20;

COMMENT ON COLUMN calibration_configs.w_sports_odds IS
    'Вес sports odds anchor (bookmaker vig-free probability). Initial: 0.50.';
COMMENT ON COLUMN calibration_configs.w_retail IS
    'Вес retail contrarian фактора (отрицательный = fade retail). Initial: -0.20.';
