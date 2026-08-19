-- 20260408074732_disable_dead_strategies.sql
-- Description: Удалить стратегии без сигналов на реальных данных:
--   sports_yes_micro  — days<1 фильтр убивает все сигналы (17% рынков <1d)
--   whale_contra      — whale_raw<-0.6 недостижимо (max на датасете -0.021)
--   sports_whale_yes  — 90% спортивных рынков без whale данных

UPDATE strategy_state
SET stage = 'disabled'
WHERE strategy_key IN ('sports_yes_micro', 'whale_contra', 'sports_whale_yes');

