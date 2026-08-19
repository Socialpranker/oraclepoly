-- 20260408163807_rename_paper_columns_to_dry.sql
-- Переименовываем колонки paper_* → dry_* в strategy_state.
-- Обновляем триггеры fn_create_bankroll_for_strategy и fn_handle_bankroll_on_stage_change
-- чтобы они принимали 'dry' вместо 'paper'.

-- 1. Переименовать колонки
ALTER TABLE strategy_state RENAME COLUMN paper_wr TO dry_wr;
ALTER TABLE strategy_state RENAME COLUMN paper_pnl TO dry_pnl;
ALTER TABLE strategy_state RENAME COLUMN paper_trades TO dry_trades;
ALTER TABLE strategy_state RENAME COLUMN paper_pf TO dry_pf;

-- 2. Пересоздать триггер INSERT: dry вместо paper
CREATE OR REPLACE FUNCTION fn_create_bankroll_for_strategy()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.stage IN ('dry', 'backtest') THEN
    INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
    VALUES (NEW.strategy_key, 500.0, 500.0, 250.0, 0.10)
    ON CONFLICT (strategy_key) DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 3. Пересоздать триггер UPDATE stage: dry вместо paper
CREATE OR REPLACE FUNCTION fn_handle_bankroll_on_stage_change()
RETURNS TRIGGER AS $$
BEGIN
  IF OLD.stage != 'live' AND NEW.stage = 'live' THEN
    DELETE FROM strategy_bankroll WHERE strategy_key = NEW.strategy_key;
  END IF;
  IF OLD.stage = 'live' AND NEW.stage IN ('dry', 'backtest') THEN
    INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
    VALUES (NEW.strategy_key, 500.0, 500.0, 250.0, 0.10)
    ON CONFLICT (strategy_key) DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

