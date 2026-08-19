-- Migration: add_contra_bias_strategies
-- Добавить 4 contra_bias стратегии найденных через discovery на VPS (2026-04-15).
-- Источник: research/strategy_discovery.py, CONTRA_BIAS_GRID на 532K HF-рынков.
-- Стратегии работают без oracle_score — только price + whale + horizon + z_posterior.

-- cb_sports_short_z-_lowvol: Sports NO, no_wh, short <7d, z<0, vol<1K, price 50-60%
-- ожидаемый PF: 1.51 (HF-бэктест)
INSERT INTO strategy_state (strategy_key, stage, filters, source, signal_source)
VALUES (
    'cb_sports_short_z-_lowvol',
    'dry',
    '{"side": "NO", "category": "Sports", "has_whale_tx": false, "horizon_zone": "short", "z_sign": -1, "max_volume_24h": 1000, "price_zone": "50-60%"}'::jsonb,
    'discovery',
    'contra_bias'
)
ON CONFLICT (strategy_key) DO UPDATE
    SET stage = EXCLUDED.stage,
        filters = EXCLUDED.filters,
        signal_source = EXCLUDED.signal_source;

-- cb_sports_short_z-: Sports NO, no_wh, short <7d, z<0, price 50-60%
-- ожидаемый PF: 1.37 (HF-бэктест)
INSERT INTO strategy_state (strategy_key, stage, filters, source, signal_source)
VALUES (
    'cb_sports_short_z-',
    'dry',
    '{"side": "NO", "category": "Sports", "has_whale_tx": false, "horizon_zone": "short", "z_sign": -1, "price_zone": "50-60%"}'::jsonb,
    'discovery',
    'contra_bias'
)
ON CONFLICT (strategy_key) DO UPDATE
    SET stage = EXCLUDED.stage,
        filters = EXCLUDED.filters,
        signal_source = EXCLUDED.signal_source;

-- cb_sports_short: Sports NO, no_wh, short <7d, price 50-60%
-- ожидаемый PF: 1.31 (HF-бэктест)
INSERT INTO strategy_state (strategy_key, stage, filters, source, signal_source)
VALUES (
    'cb_sports_short',
    'dry',
    '{"side": "NO", "category": "Sports", "has_whale_tx": false, "horizon_zone": "short", "price_zone": "50-60%"}'::jsonb,
    'discovery',
    'contra_bias'
)
ON CONFLICT (strategy_key) DO UPDATE
    SET stage = EXCLUDED.stage,
        filters = EXCLUDED.filters,
        signal_source = EXCLUDED.signal_source;

-- cb_politics_short: Politics NO, no_wh, short <7d, price 50-60%
-- ожидаемый PF: 2.68 (мало данных в HF — нужно накопить dry)
INSERT INTO strategy_state (strategy_key, stage, filters, source, signal_source)
VALUES (
    'cb_politics_short',
    'dry',
    '{"side": "NO", "category": "Politics", "has_whale_tx": false, "horizon_zone": "short", "price_zone": "50-60%"}'::jsonb,
    'discovery',
    'contra_bias'
)
ON CONFLICT (strategy_key) DO UPDATE
    SET stage = EXCLUDED.stage,
        filters = EXCLUDED.filters,
        signal_source = EXCLUDED.signal_source;

-- Инициализировать bankroll записи (если нет — создать)
INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll)
VALUES
    ('cb_sports_short_z-_lowvol', 10000, 10000),
    ('cb_sports_short_z-',        10000, 10000),
    ('cb_sports_short',           10000, 10000),
    ('cb_politics_short',         10000, 10000)
ON CONFLICT (strategy_key) DO NOTHING;
