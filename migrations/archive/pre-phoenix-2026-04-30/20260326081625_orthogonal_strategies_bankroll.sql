-- 20260326081625_orthogonal_strategies_bankroll.sql
-- Bankroll для 3 ортогональных стратегий (data-driven, validated on 937K snapshots)

INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
VALUES
  ('sports_mid_horizon', 1000, 1000, 500, 0.10),
  ('fast_no_bias', 1000, 1000, 500, 0.10),
  ('sports_whale_yes', 1000, 1000, 500, 0.10)
ON CONFLICT (strategy_key) DO NOTHING;
