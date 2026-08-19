-- 20260406160000_missing_strategy_bankroll.sql
-- Bankroll для стратегий, добавленных в strategy_state без записи в strategy_bankroll.
-- Bankroll = initial + накопленный PnL из strategy_positions на момент миграции.

INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
VALUES
  ('baseline',           1263.29, 1000.00, 500, 0.10),
  ('crypto_no_bias_5_8',  502.85,  500.00, 250, 0.10),
  ('politics_no_bias',    620.57,  500.00, 250, 0.10),
  ('sports_yes_micro',    500.00,  500.00, 250, 0.10),
  ('weather_arb',         500.00,  500.00, 250, 0.10)
ON CONFLICT (strategy_key) DO NOTHING;
