-- Очистка bankroll disabled стратегий
-- 28 disabled стратегий удерживают $14,500 (bankroll нетронутый).
-- Обнуляем bankroll и initial_bankroll для disabled, чтобы не засорять portfolio_money_quotas.
-- Перераспределяем пул: dry стратегии получат $500 initial_bankroll (уже есть), _portfolio остаётся $10K.

BEGIN;

-- 1. Обнулить bankroll у disabled стратегий (деньги виртуальные, позиций нет)
UPDATE strategy_bankroll
SET bankroll = 0,
    initial_bankroll = 0,
    updated_at = NOW()
WHERE strategy_key IN (
    SELECT strategy_key FROM strategy_state WHERE stage = 'disabled'
);

-- 2. Удалить stale записи disabled стратегий из strategy_bankroll, которые больше не нужны
-- НЕ удаляем, просто обнуляем — удаление может сломать JOIN'ы в quota_refresh drift detection

COMMIT;
