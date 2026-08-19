-- 20260408163425_remove_disabled_strategies.sql
-- Удаляем disabled стратегии (baseline, sports_whale_yes, sports_yes_micro, whale_contra).
-- Они не генерируют сигналы и не используются. Виртуальный dry_run ордер sports_whale_yes тоже удаляем.

DELETE FROM execution_orders WHERE strategy_key IN ('baseline','sports_whale_yes','sports_yes_micro','whale_contra') AND status = 'dry_run';
DELETE FROM strategy_state WHERE stage = 'disabled';
DELETE FROM strategy_bankroll WHERE strategy_key IN ('baseline','sports_whale_yes','sports_yes_micro','whale_contra');

