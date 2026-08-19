-- etl_runs: audit log for all feature_store ETL job executions.

CREATE TABLE IF NOT EXISTS etl_runs (
    id BIGSERIAL PRIMARY KEY,
    job_name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running','success','failed')),
    rows_processed INTEGER DEFAULT 0,
    rows_inserted INTEGER DEFAULT 0,
    rows_updated INTEGER DEFAULT 0,
    error_message TEXT,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_etl_runs_job_time ON etl_runs (job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_etl_runs_status ON etl_runs (status, started_at DESC);

COMMENT ON TABLE etl_runs IS
  'Audit trail for feature_store ETL jobs: bulk_load, refresh, audit, upstream_check.';
