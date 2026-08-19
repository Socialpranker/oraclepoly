-- Инициализация bankroll для whale_contra
-- Guard: ⚪ НОВАЯ — backtest 96 markets, ROI +106.7%, PF 4.20
-- Проблема: 0 live signals (все подходящие markets entry < 15%)
INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
VALUES ('whale_contra', 500.00, 500.00, 500.00, 0.10)
ON CONFLICT (strategy_key) DO NOTHING;
