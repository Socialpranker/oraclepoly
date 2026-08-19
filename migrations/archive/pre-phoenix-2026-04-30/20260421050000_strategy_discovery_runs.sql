-- strategy_discovery_runs: audit log для каждого запуска strategy_discovery.
-- Соединяет зарегистрированные стратегии со своим discovery run
-- (откуда родились, на каких данных, какие пороги прошли).

CREATE TABLE IF NOT EXISTS strategy_discovery_runs (
    id BIGSERIAL PRIMARY KEY,
    run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source TEXT NOT NULL,          -- 'feature_store' | 'parquet' | 'hybrid'
    fs_source TEXT,                     -- 'live' | 'hf' | 'all' | NULL
    total_combos INTEGER NOT NULL,
    passed_filter INTEGER NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    auto_registered INTEGER NOT NULL DEFAULT 0,
    data_window_from TIMESTAMPTZ,
    data_window_to TIMESTAMPTZ,
    rows_loaded INTEGER,
    meta JSONB
);

CREATE INDEX IF NOT EXISTS idx_discovery_runs_time
    ON strategy_discovery_runs (run_at DESC);

-- Тонкая связка strategy_state → discovery run.
-- Nullable, так как существующие 84+ стратегий были созданы до этой миграции.
ALTER TABLE strategy_state
    ADD COLUMN IF NOT EXISTS discovery_run_id BIGINT REFERENCES strategy_discovery_runs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS discovery_metrics JSONB;
