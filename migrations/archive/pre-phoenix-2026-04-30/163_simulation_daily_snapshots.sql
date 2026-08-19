-- 163_simulation_daily_snapshots.sql
-- Ежедневные снапшоты результатов симуляции (Mode C/D/E/L)

CREATE TABLE IF NOT EXISTS simulation_daily_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date DATE NOT NULL,
    model TEXT NOT NULL,

    initial_bank DOUBLE PRECISION NOT NULL,
    final_bank DOUBLE PRECISION NOT NULL,
    total_pnl DOUBLE PRECISION NOT NULL,
    roi_percent NUMERIC(8, 2) NOT NULL,
    total_bets INT NOT NULL,
    wins INT NOT NULL,
    losses INT NOT NULL,
    win_rate NUMERIC(5, 2) NOT NULL,
    max_drawdown DOUBLE PRECISION NOT NULL,
    sharpe_ratio NUMERIC(8, 4),
    sortino_ratio NUMERIC(8, 4),
    profit_factor NUMERIC(8, 2),
    ev_per_bet NUMERIC(10, 2),
    max_losing_streak INT,

    params JSONB NOT NULL,
    details JSONB,

    snapshots_used INT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(date, model)
);

CREATE INDEX IF NOT EXISTS idx_sim_snapshots_date ON simulation_daily_snapshots(date DESC);
CREATE INDEX IF NOT EXISTS idx_sim_snapshots_model_date ON simulation_daily_snapshots(model, date DESC);

COMMENT ON TABLE simulation_daily_snapshots IS 'Ежедневные снапшоты результатов симуляции (Mode C/D/E/L) для отслеживания перформанса моделей';
