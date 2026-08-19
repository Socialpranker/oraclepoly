-- 151: Статистика ставок для оценки банкролла
ALTER TABLE whales ADD COLUMN IF NOT EXISTS avg_bet_size NUMERIC DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS max_bet_size NUMERIC DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS bet_count INTEGER DEFAULT 0;

-- Заполнить из существующих транзакций
UPDATE whales w SET
    avg_bet_size = sub.avg_amount,
    max_bet_size = sub.max_amount,
    bet_count = sub.cnt
FROM (
    SELECT whale_id,
           AVG(amount) AS avg_amount,
           MAX(amount) AS max_amount,
           COUNT(*) AS cnt
    FROM transactions
    WHERE whale_id IS NOT NULL AND is_arbitrage = false
    GROUP BY whale_id
) sub
WHERE w.id = sub.whale_id;
