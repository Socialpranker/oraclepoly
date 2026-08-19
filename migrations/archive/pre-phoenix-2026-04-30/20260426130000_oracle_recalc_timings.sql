-- 20260426130000_oracle_recalc_timings.sql
-- Description: Phase 0 instrumentation для аудита oracle-recalc bottleneck'ов.
-- Цель: собрать P50/P95 latency каждого этапа в pipeline (см.
-- memory/oracle_recalculate_audit_2026-04-26.md). Read-only observability,
-- никаких изменений в логике oracle calculator.

CREATE TABLE IF NOT EXISTS oracle_recalc_timings (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL,             -- группирует все timings одного cron-запуска
    market_id UUID,                    -- NULL для global этапов (prefetch_books и т.п.)
    step TEXT NOT NULL,                -- "fetch_market", "fetch_oi", "smart_money", ...
    duration_ms NUMERIC(10,2) NOT NULL,
    error TEXT,                        -- non-NULL если этап завершился исключением
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Для агрегаций по step — главный access pattern (P50/P95 per step за run)
CREATE INDEX IF NOT EXISTS idx_oracle_recalc_timings_run_step
    ON oracle_recalc_timings (run_id, step);

-- Для analytics: timeseries трендов за 7+ дней
CREATE INDEX IF NOT EXISTS idx_oracle_recalc_timings_step_created
    ON oracle_recalc_timings (step, created_at DESC);

-- Retention: 30 дней должно хватить для baseline + проверки post-optimization.
-- Cleanup как cron-задача отдельно (не в этой миграции).

COMMENT ON TABLE oracle_recalc_timings IS
    'Phase 0 observability для oracle-recalc audit. См. memory/oracle_recalculate_audit_2026-04-26.md';
