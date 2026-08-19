-- migrations/20260408000001_portfolio_bankroll.sql
-- Инициализация _portfolio записи в strategy_bankroll.
-- _portfolio — общий пул dry trading. Per-strategy записи остаются для трекинга ROI.
-- FK: strategy_bankroll.strategy_key → strategy_state.strategy_key

INSERT INTO strategy_state (strategy_key, stage, updated_at)
VALUES ('_portfolio', 'system', now())
ON CONFLICT (strategy_key) DO UPDATE SET stage = 'system', updated_at = now();

INSERT INTO strategy_bankroll (
    strategy_key,
    bankroll,
    initial_bankroll,
    total_withdrawn,
    total_reinvested,
    daily_limit,
    reinvest_rate,
    stage,
    updated_at
)
VALUES (
    '_portfolio',
    10000.00,
    10000.00,
    0.00,
    0.00,
    9999999.00,
    0.00,
    'paper',
    now()
)
ON CONFLICT (strategy_key) DO UPDATE SET
    bankroll = 10000.00,
    initial_bankroll = 10000.00,
    daily_limit = 9999999.00,
    reinvest_rate = 0.00,
    updated_at = now();
