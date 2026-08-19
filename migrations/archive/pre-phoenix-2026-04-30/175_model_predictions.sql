-- migrations/175_model_predictions.sql

-- Реестр моделей (статический, заполняется при деплое)
CREATE TABLE IF NOT EXISTS model_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    description TEXT,
    feature_flags JSONB NOT NULL DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    is_production BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Предсказания каждой модели
CREATE TABLE IF NOT EXISTS model_predictions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id UUID NOT NULL REFERENCES model_registry(id),
    market_id UUID NOT NULL REFERENCES markets(id),
    oracle_score NUMERIC(6, 4) NOT NULL,
    z_posterior NUMERIC(10, 4),
    market_price NUMERIC(6, 4),
    divergence NUMERIC(6, 4),
    contributions JSONB,
    signal_count INTEGER DEFAULT 0,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_model_pred_model_market
    ON model_predictions(model_id, market_id);
CREATE INDEX IF NOT EXISTS idx_model_pred_computed
    ON model_predictions(computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_pred_market
    ON model_predictions(market_id, computed_at DESC);

-- Seed model_registry
INSERT INTO model_registry (model_name, version, description, feature_flags, is_active, is_production)
VALUES
    ('baseline', 'v2.0', 'Текущая система без изменений', '{}', TRUE, TRUE),
    ('goldsky_pnl', 'v2.1', 'Goldsky PnL + Positions для credibility', '{"use_goldsky_pnl": true}', TRUE, FALSE),
    ('taker_weight', 'v2.2', 'Maker/Taker split для whale weight', '{"use_taker_weight": true}', TRUE, FALSE),
    ('sampling_oi', 'v2.3', 'Real OI + Sampling Markets filter', '{"use_sampling_filter": true}', TRUE, FALSE),
    ('comments_sentiment', 'v2.4', 'Comments holders-only sentiment', '{"use_comments_sentiment": true}', TRUE, FALSE),
    ('all_features', 'v3.0', 'Все v2.1-v2.4 combined', '{"use_goldsky_pnl": true, "use_taker_weight": true, "use_sampling_filter": true, "use_comments_sentiment": true}', TRUE, FALSE)
ON CONFLICT (model_name) DO NOTHING;
