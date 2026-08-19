-- 20260406180000_bankroll_paper_only.sql
-- Bankroll — только для paper/backtest стратегий.
-- Live-стратегии используют реальный баланс Polymarket из execution_orders.

-- 1. Trigger на INSERT: создавать bankroll только для paper/backtest
CREATE OR REPLACE FUNCTION fn_create_bankroll_for_strategy()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.stage IN ('paper', 'backtest') THEN
    INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
    VALUES (NEW.strategy_key, 500.0, 500.0, 250.0, 0.10)
    ON CONFLICT (strategy_key) DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 2. Trigger на UPDATE stage: paper→live удаляет bankroll, live→paper создаёт
CREATE OR REPLACE FUNCTION fn_handle_bankroll_on_stage_change()
RETURNS TRIGGER AS $$
BEGIN
  IF OLD.stage != 'live' AND NEW.stage = 'live' THEN
    DELETE FROM strategy_bankroll WHERE strategy_key = NEW.strategy_key;
  END IF;
  IF OLD.stage = 'live' AND NEW.stage IN ('paper', 'backtest') THEN
    INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
    VALUES (NEW.strategy_key, 500.0, 500.0, 250.0, 0.10)
    ON CONFLICT (strategy_key) DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bankroll_on_stage_change ON strategy_state;
CREATE TRIGGER trg_bankroll_on_stage_change
  AFTER UPDATE OF stage ON strategy_state
  FOR EACH ROW
  EXECUTE FUNCTION fn_handle_bankroll_on_stage_change();

-- 3. Удалить мусорные bankroll записи для live-стратегий
-- (strategy_settle, resolve_outcomes читают только strategy_positions — это всегда paper)
DELETE FROM strategy_bankroll sb
WHERE EXISTS (
  SELECT 1 FROM strategy_state ss
  WHERE ss.strategy_key = sb.strategy_key
    AND ss.stage = 'live'
);
