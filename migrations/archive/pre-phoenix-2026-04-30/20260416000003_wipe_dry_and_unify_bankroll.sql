-- 20260416000003_wipe_dry_and_unify_bankroll.sql
-- Полная очистка dry-состояния: удаляем все позиции, ledger, обнуляем dry-метрики
-- и унифицируем per-strategy bankroll до $10K/$10K.
--
-- Причина: после восстановления 6 oracle-стратегий (migration 000002) в БД
-- остался разнобой — часть стратегий на $10K, часть на $500 (legacy), часть на $0.
-- Старые open-позиции держали слоты и искажали quota-логику (sports_no_baseline
-- занял 50 слотов MIN_QUOTA, блокируя новые стратегии). Полный wipe → чистый старт.
--
-- Portfolio bankroll (_portfolio) НЕ трогаем — это SSoT всего банкрола.

BEGIN;

-- 1. Удалить ledger (FK NO ACTION — руками до позиций)
DELETE FROM strategy_ledger
WHERE position_id IN (SELECT id FROM strategy_positions WHERE mode = 'dry');

-- 2. Удалить все dry-позиции (open / closed / cancelled / void — полный wipe)
DELETE FROM strategy_positions WHERE mode = 'dry';

-- 3. Обнулить dry-метрики на strategy_state (пересчёт с нуля при strategy-monitor)
UPDATE strategy_state
SET dry_wr = NULL,
    dry_pnl = NULL,
    dry_trades = NULL,
    dry_pf = NULL,
    updated_at = NOW()
WHERE stage IN ('dry', 'disabled');

-- 4. Унификация bankroll для активных dry-стратегий: всем $10K/$10K.
--    INSERT для отсутствующих, UPDATE для существующих.
INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, stage)
SELECT strategy_key, 10000, 10000, 'dry'
FROM strategy_state
WHERE stage IN ('dry', 'live')
ON CONFLICT (strategy_key) DO UPDATE
    SET bankroll = 10000,
        initial_bankroll = 10000,
        total_withdrawn = 0,
        total_reinvested = 0,
        stage = EXCLUDED.stage,
        updated_at = NOW();

-- 5. Диагностика
DO $$
DECLARE
    dry_positions INT;
    dry_ledger INT;
    active_strategies INT;
    bankroll_sum NUMERIC;
BEGIN
    SELECT COUNT(*) INTO dry_positions FROM strategy_positions WHERE mode = 'dry';
    SELECT COUNT(*) INTO dry_ledger FROM strategy_ledger sl
      JOIN strategy_positions sp ON sl.position_id = sp.id WHERE sp.mode = 'dry';
    SELECT COUNT(*) INTO active_strategies FROM strategy_state WHERE stage IN ('dry','live');
    SELECT SUM(bankroll) INTO bankroll_sum FROM strategy_bankroll
      WHERE stage IN ('dry','live') AND strategy_key != '_portfolio';
    RAISE NOTICE 'Dry positions remaining: %', dry_positions;
    RAISE NOTICE 'Dry ledger remaining: %', dry_ledger;
    RAISE NOTICE 'Active strategies: %', active_strategies;
    RAISE NOTICE 'Sum of per-strategy bankroll: %', bankroll_sum;
END $$;

COMMIT;
