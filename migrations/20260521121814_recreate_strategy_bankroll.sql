-- 20260521121814_recreate_strategy_bankroll.sql
-- Description: Recreate strategy_bankroll table (потеряна при переезде на новый сервер).
--
-- Issue (2026-05-21): promotion_readiness endpoint падает с UndefinedTableError,
-- что блокирует Shadow tab в dashboard. Таблица описана в Phoenix schema
-- (migrations/00000000000000_phoenix_schema_2026_04_30.sql:833) но при cutover
-- на новом сервере не была восстановлена.
--
-- Содержимое: bankroll/staged каждой стратегии. Seed для существующих детекторов
-- из detector_state. stage = 'live' если detector live, иначе 'dry'.

CREATE TABLE IF NOT EXISTS strategy_bankroll (
    strategy_key text NOT NULL PRIMARY KEY,
    bankroll numeric(12,2) DEFAULT 10000 NOT NULL,
    initial_bankroll numeric(12,2) DEFAULT 10000 NOT NULL,
    total_withdrawn numeric(12,2) DEFAULT 0 NOT NULL,
    total_reinvested numeric(12,2) DEFAULT 0 NOT NULL,
    daily_limit numeric(10,2) DEFAULT 500 NOT NULL,
    reinvest_rate numeric(4,2) DEFAULT 0.10 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    stage text DEFAULT 'dry' NOT NULL
);

-- Seed: один row на каждый известный детектор. stage берётся из detector_state.
-- 'live' остаётся 'live', всё остальное → 'dry' (промоушн в live делается только manual через TG).
INSERT INTO strategy_bankroll (strategy_key, stage)
SELECT detector, CASE WHEN stage = 'live' THEN 'live' ELSE 'dry' END
FROM detector_state
ON CONFLICT (strategy_key) DO NOTHING;

COMMENT ON TABLE strategy_bankroll IS 'Bankroll per strategy/detector. Recreated 2026-05-21 after migration loss.';
