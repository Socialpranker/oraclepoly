-- Strategy state: единственный источник стадии стратегии (backtest/paper/live/disabled)
CREATE TABLE IF NOT EXISTS strategy_state (
    strategy_key TEXT PRIMARY KEY,
    stage TEXT NOT NULL DEFAULT 'backtest',
    promoted_at TIMESTAMPTZ,
    disabled_reason TEXT,

    -- Backtest метрики
    backtest_wr NUMERIC(5,2),
    backtest_pnl NUMERIC(10,2),
    backtest_trades INTEGER,
    backtest_pf NUMERIC(6,2),
    backtest_max_dd NUMERIC(5,2),
    backtest_at TIMESTAMPTZ,

    -- Paper метрики
    paper_wr NUMERIC(5,2),
    paper_pnl NUMERIC(10,2),
    paper_trades INTEGER,
    paper_pf NUMERIC(6,2),

    -- Live метрики
    live_wr NUMERIC(5,2),
    live_pnl NUMERIC(10,2),
    live_trades INTEGER,

    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Аудит-лог переходов
CREATE TABLE IF NOT EXISTS strategy_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_key TEXT NOT NULL,
    event TEXT NOT NULL,
    details JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_strategy_audit_key ON strategy_audit(strategy_key);
CREATE INDEX IF NOT EXISTS idx_strategy_audit_created ON strategy_audit(created_at DESC);
