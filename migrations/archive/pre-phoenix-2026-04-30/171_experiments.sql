-- 171: Experiments — Trading Simulation Tournaments

CREATE TABLE IF NOT EXISTS experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    initial_bank NUMERIC(12,2) NOT NULL DEFAULT 10000,
    reinvest_rate NUMERIC(5,4) NOT NULL DEFAULT 0.10,
    duration_days INT NOT NULL DEFAULT 7,
    fee_profile VARCHAR(20) NOT NULL DEFAULT 'direct',
    kelly_fraction NUMERIC(5,4) NOT NULL DEFAULT 0.25,
    max_event_exposure NUMERIC(5,4) DEFAULT 0.10,
    max_category_exposure NUMERIC(5,4) DEFAULT 0.30,
    category_filter JSONB DEFAULT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS experiment_models (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id UUID NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    model_key VARCHAR(50) NOT NULL,
    allocated_bank NUMERIC(12,2) NOT NULL,
    current_bank NUMERIC(12,2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(experiment_id, model_key)
);

CREATE INDEX IF NOT EXISTS idx_experiment_models_experiment
    ON experiment_models(experiment_id);

CREATE TABLE IF NOT EXISTS experiment_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id UUID NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    model_key VARCHAR(50) NOT NULL,
    snapshot_date DATE NOT NULL,
    bank_start NUMERIC(12,2) NOT NULL,
    bank_end NUMERIC(12,2) NOT NULL,
    pnl NUMERIC(12,2) NOT NULL,
    reinvested NUMERIC(12,2) DEFAULT 0,
    withdrawn NUMERIC(12,2) DEFAULT 0,
    bets_count INT DEFAULT 0,
    win_rate NUMERIC(5,4),
    UNIQUE(experiment_id, model_key, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_experiment_snapshots_lookup
    ON experiment_snapshots(experiment_id, snapshot_date);
