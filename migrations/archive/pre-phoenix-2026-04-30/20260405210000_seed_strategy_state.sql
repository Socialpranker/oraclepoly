-- Seed strategy_state для 12 существующих стратегий.
-- Все начинают в stage=paper (кроме baseline — сразу live с минимальным stake).
-- Стратегии с live P&L данными (fast_no_bias) можно вручную перевести в live.

INSERT INTO strategy_state (strategy_key, stage) VALUES
    ('fast_no_bias', 'paper'),
    ('sports_mid_horizon', 'paper'),
    ('clean_oracle', 'paper'),
    ('sports_whale_yes', 'paper'),
    ('whale_contra', 'paper'),
    ('crypto_no_bias', 'paper'),
    ('crypto_no_bias_5_8', 'paper'),
    ('politics_no_bias', 'paper'),
    ('sports_yes_micro', 'paper'),
    ('sniper', 'paper'),
    ('timing_edge', 'paper'),
    ('baseline', 'paper')
ON CONFLICT (strategy_key) DO NOTHING;

-- Audit log для seed
INSERT INTO strategy_audit (strategy_key, event, details) VALUES
    ('baseline', 'seed_initial', '{"note": "Phase 2 seed, all strategies start in paper"}');
