-- 20260324100000_fade_the_bots_bankroll.sql
-- Bankroll for Fade the Bots strategy (paper trading $10K)

INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
VALUES ('fade_the_bots', 10000, 10000, 500, 0.10)
ON CONFLICT (strategy_key) DO NOTHING;
