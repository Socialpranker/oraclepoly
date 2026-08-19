-- 187: Unified Strategy Positions

-- Позиции
CREATE TABLE IF NOT EXISTS strategy_positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_key TEXT NOT NULL,
    market_id UUID NOT NULL,
    event_id UUID,
    side TEXT NOT NULL,
    entry_price NUMERIC(8,4) NOT NULL,
    oracle_score_at_entry NUMERIC(6,4),
    divergence_at_entry NUMERIC(6,4),
    stake NUMERIC(10,2) NOT NULL,
    category TEXT,
    question TEXT,
    meta JSONB DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    pnl NUMERIC(10,2),
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    snapshot_date DATE NOT NULL DEFAULT CURRENT_DATE,
    UNIQUE(strategy_key, market_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_sp_strategy_status ON strategy_positions(strategy_key, status);
CREATE INDEX IF NOT EXISTS idx_sp_market ON strategy_positions(market_id);
CREATE INDEX IF NOT EXISTS idx_sp_open ON strategy_positions(status) WHERE status = 'open';

-- Банкролл per strategy
CREATE TABLE IF NOT EXISTS strategy_bankroll (
    strategy_key TEXT PRIMARY KEY,
    bankroll NUMERIC(12,2) NOT NULL DEFAULT 1000,
    initial_bankroll NUMERIC(12,2) NOT NULL DEFAULT 1000,
    total_withdrawn NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_reinvested NUMERIC(12,2) NOT NULL DEFAULT 0,
    daily_limit NUMERIC(10,2) NOT NULL DEFAULT 500,
    reinvest_rate NUMERIC(4,2) NOT NULL DEFAULT 0.10,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ledger
CREATE TABLE IF NOT EXISTS strategy_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_key TEXT NOT NULL,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    event_type TEXT NOT NULL,
    position_id UUID REFERENCES strategy_positions(id),
    amount NUMERIC(10,2) NOT NULL,
    balance_after NUMERIC(12,2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sl_strategy_date ON strategy_ledger(strategy_key, date DESC);

-- Seed bankroll
INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
VALUES
    ('div_sweet_spot', 1000, 1000, 500, 0.10),
    ('category_specialist', 1000, 1000, 500, 0.10),
    ('signal_consensus', 1000, 1000, 500, 0.10),
    ('calibration_arbitrage', 1000, 1000, 500, 0.10),
    ('elite_whale_copy', 1000, 1000, 500, 0.10),
    ('model_ensemble', 1000, 1000, 500, 0.10),
    ('anti_overconfidence', 1000, 1000, 500, 0.10),
    ('regime_switcher', 1000, 1000, 500, 0.10),
    ('cwi', 1000, 1000, 500, 0.10)
ON CONFLICT (strategy_key) DO NOTHING;
