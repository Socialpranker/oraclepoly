-- migrations/20260410000003_oracle_new_factors.sql
-- Phase 2: новые колонки для maker_direction и taker_imbalance факторов
-- в oracle_snapshots + веса в calibration_configs.

-- oracle_snapshots: raw значения новых факторов (Phase 2)
ALTER TABLE oracle_snapshots
    ADD COLUMN IF NOT EXISTS maker_direction_raw   NUMERIC(5,4) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS taker_imbalance_raw   NUMERIC(5,4) DEFAULT 0;

-- Индекс для быстрого поиска по maker_direction_raw (calibration grid search)
CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_maker_direction
    ON oracle_snapshots (maker_direction_raw)
    WHERE maker_direction_raw != 0;

-- calibration_configs: новые веса факторов
ALTER TABLE calibration_configs
    ADD COLUMN IF NOT EXISTS w_maker_direction  NUMERIC(6,4) DEFAULT  0.40,
    ADD COLUMN IF NOT EXISTS w_taker_imbalance  NUMERIC(6,4) DEFAULT  0.15;

-- Комментарии
COMMENT ON COLUMN oracle_snapshots.maker_direction_raw IS
    'maker_direction_bayes_factor raw value (из maker_direction_cache, Phase 2)';
COMMENT ON COLUMN oracle_snapshots.taker_imbalance_raw IS
    'taker_imbalance_bayes_factor raw value (из market_signals, Phase 2)';
COMMENT ON COLUMN calibration_configs.w_maker_direction IS
    'Вес maker direction фактора. Initial: 0.40, corr +0.75 с outcome.';
COMMENT ON COLUMN calibration_configs.w_taker_imbalance IS
    'Вес taker imbalance (scale contrarian factor). Initial: +0.15 (sign уже в factor).';
