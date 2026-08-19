-- Ежедневные бенчмарки Oracle Score.
-- Cron job сохраняет метрики каждый день для отслеживания динамики.

CREATE TABLE IF NOT EXISTS daily_benchmarks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date DATE NOT NULL UNIQUE,
    total_resolved INT NOT NULL DEFAULT 0,
    accuracy NUMERIC(6, 4) NOT NULL DEFAULT 0,
    avg_brier_score NUMERIC(6, 4) NOT NULL DEFAULT 0,
    pending_count INT NOT NULL DEFAULT 0,
    total_snapshots INT NOT NULL DEFAULT 0,
    -- Метрики по периодам (accuracy за последние N дней на момент записи)
    accuracy_1d NUMERIC(6, 4),
    accuracy_3d NUMERIC(6, 4),
    accuracy_7d NUMERIC(6, 4),
    accuracy_14d NUMERIC(6, 4),
    accuracy_30d NUMERIC(6, 4),
    -- Brier по периодам
    brier_1d NUMERIC(6, 4),
    brier_3d NUMERIC(6, 4),
    brier_7d NUMERIC(6, 4),
    brier_14d NUMERIC(6, 4),
    brier_30d NUMERIC(6, 4),
    -- Количество resolved за каждый период
    resolved_1d INT DEFAULT 0,
    resolved_3d INT DEFAULT 0,
    resolved_7d INT DEFAULT 0,
    resolved_14d INT DEFAULT 0,
    resolved_30d INT DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_daily_benchmarks_date ON daily_benchmarks(date DESC);
