-- trade_signals — каждый положительный match стратегии (без учёта лимитов).
-- Бэктест = JOIN с mv_market_resolved_outcome. Forward-testing из коробки.

CREATE TABLE IF NOT EXISTS trade_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_key TEXT NOT NULL,
    market_id UUID NOT NULL,
    event_id UUID,
    side TEXT NOT NULL,

    -- Цены и метрики на момент сигнала
    entry_price NUMERIC(8,4) NOT NULL,
    oracle_score NUMERIC(6,4) NOT NULL,
    market_price NUMERIC(8,4) NOT NULL,
    divergence NUMERIC(6,4) NOT NULL,
    ev NUMERIC(8,4),

    -- Факторы (полный snapshot)
    whale_contribution_raw NUMERIC(8,4),
    sm_registry_raw NUMERIC(8,4),
    whale_tx_count INTEGER,
    micro_factor_raw NUMERIC(8,4),
    z_posterior NUMERIC(8,4),
    news_contribution_raw NUMERIC(8,4),
    volume_24h NUMERIC(14,2),

    -- Классификация
    category TEXT,
    end_date TIMESTAMPTZ,
    tier INTEGER,
    expected_accuracy NUMERIC(6,4),

    -- Мета
    question TEXT,
    snapshot_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(strategy_key, market_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_trade_signals_strategy ON trade_signals(strategy_key);
CREATE INDEX IF NOT EXISTS idx_trade_signals_market ON trade_signals(market_id);
CREATE INDEX IF NOT EXISTS idx_trade_signals_date ON trade_signals(snapshot_date);
