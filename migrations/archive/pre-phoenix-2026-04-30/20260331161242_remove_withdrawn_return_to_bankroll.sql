-- Убираем механику withdrawn: вся прибыль остаётся в bankroll.
-- Возвращаем ранее "выведенные" средства обратно в bankroll.

BEGIN;

-- 1. Вернуть withdrawn обратно в bankroll
UPDATE strategy_bankroll
SET bankroll = bankroll + total_withdrawn,
    total_withdrawn = 0,
    total_reinvested = 0,
    updated_at = now();

COMMIT;
