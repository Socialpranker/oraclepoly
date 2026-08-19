-- 160: whale_tier — 8-уровневая сегментация китов
ALTER TABLE whales ADD COLUMN IF NOT EXISTS whale_tier TEXT DEFAULT 'shrimp';
CREATE INDEX IF NOT EXISTS idx_whales_tier ON whales(whale_tier);

-- Backfill из avg_bet_size
UPDATE whales SET whale_tier = CASE
    WHEN COALESCE(avg_bet_size, 0) = 0 THEN 'shrimp'
    WHEN bet_count < 3 THEN
        CASE
            WHEN avg_bet_size >= 10000 THEN 'fish'
            WHEN avg_bet_size >= 7500 THEN 'crab'
            ELSE 'shrimp'
        END
    WHEN avg_bet_size >= 1000000 THEN 'leviathan'
    WHEN avg_bet_size >= 250000 THEN 'orca'
    WHEN avg_bet_size >= 100000 THEN 'whale'
    WHEN avg_bet_size >= 50000 THEN 'shark'
    WHEN avg_bet_size >= 20000 THEN 'dolphin'
    WHEN avg_bet_size >= 10000 THEN 'fish'
    WHEN avg_bet_size >= 7500 THEN 'crab'
    ELSE 'shrimp'
END
WHERE total_volume > 0;
