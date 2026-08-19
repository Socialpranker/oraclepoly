-- 20260325133909_cluster_strategy_bankroll.sql
-- Description: Bankroll seed для 3 новых cluster стратегий

-- Bankroll для 3 новых cluster стратегий
INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
VALUES
    ('sniper', 1000, 1000, 200, 0.10),
    ('timing_edge', 1000, 1000, 500, 0.10),
    ('fade_whales', 1000, 1000, 300, 0.10)
ON CONFLICT (strategy_key) DO NOTHING;

