-- 20260407080000_live_bankroll.sql
-- Добавить stage колонку в strategy_bankroll и вести bankroll для live стратегий.
-- Initial deposit разделён поровну между стратегиями (crypto_no_bias, fast_no_bias).

-- 1. Колонка stage
ALTER TABLE strategy_bankroll ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'dry';

-- 2. Обновить существующие dry стратегии
UPDATE strategy_bankroll SET stage = 'dry' WHERE stage = 'dry';

-- 3. Вставить live стратегии с реальным initial bankroll
-- P&L из execution_orders на момент миграции
INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate, stage)
VALUES
  ('crypto_no_bias', 25.0, 25.0, 10.0, 1.0, 'live'),
  ('fast_no_bias', 25.0, 25.0, 10.0, 1.0, 'live')
ON CONFLICT (strategy_key) DO UPDATE SET
  bankroll = EXCLUDED.bankroll,
  initial_bankroll = EXCLUDED.initial_bankroll,
  daily_limit = EXCLUDED.daily_limit,
  reinvest_rate = EXCLUDED.reinvest_rate,
  stage = EXCLUDED.stage;

-- 4. Убрать триггер, который удалял bankroll при переходе в live
CREATE OR REPLACE FUNCTION fn_handle_bankroll_on_stage_change()
RETURNS TRIGGER AS $$
BEGIN
  -- paper/backtest → live: обновить stage, сохранить bankroll
  IF OLD.stage != 'live' AND NEW.stage = 'live' THEN
    UPDATE strategy_bankroll SET stage = 'live' WHERE strategy_key = NEW.strategy_key;
  END IF;
  -- live → paper/backtest: обновить stage
  IF OLD.stage = 'live' AND NEW.stage IN ('paper', 'backtest') THEN
    INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate, stage)
    VALUES (NEW.strategy_key, 500.0, 500.0, 250.0, 0.10, 'dry')
    ON CONFLICT (strategy_key) DO UPDATE SET stage = 'dry';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
