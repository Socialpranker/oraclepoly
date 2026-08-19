-- 20260406170000_link_strategy_bankroll_to_state.sql
-- Связываем strategy_bankroll с strategy_state через FK + trigger.
-- Теперь добавление стратегии в strategy_state автоматически создаёт bankroll запись.

-- 1. FK: strategy_bankroll.strategy_key → strategy_state.strategy_key
ALTER TABLE strategy_bankroll
  ADD CONSTRAINT fk_bankroll_strategy_state
  FOREIGN KEY (strategy_key) REFERENCES strategy_state (strategy_key)
  ON DELETE CASCADE;

-- 2. Trigger-функция: при INSERT в strategy_state → создать bankroll если не существует
CREATE OR REPLACE FUNCTION fn_create_bankroll_for_strategy()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
  VALUES (NEW.strategy_key, 500.0, 500.0, 250.0, 0.10)
  ON CONFLICT (strategy_key) DO NOTHING;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 3. Trigger на INSERT в strategy_state
DROP TRIGGER IF EXISTS trg_create_bankroll_on_strategy_insert ON strategy_state;
CREATE TRIGGER trg_create_bankroll_on_strategy_insert
  AFTER INSERT ON strategy_state
  FOR EACH ROW
  EXECUTE FUNCTION fn_create_bankroll_for_strategy();
