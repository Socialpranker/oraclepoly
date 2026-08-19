-- 20260424135300_strategy_live_metrics.sql
-- Description: Adaptive Kelly Sizing — per-strategy live metrics + dynamic stake
-- Заменяет LIVE_DAILY_LIMITS env + _get_live_daily_limits() в execution_job.

CREATE TABLE IF NOT EXISTS strategy_live_metrics (
    strategy_key TEXT PRIMARY KEY,
    -- Накопленные метрики
    live_resolved INT NOT NULL DEFAULT 0,
    live_wins INT NOT NULL DEFAULT 0,
    live_losses INT NOT NULL DEFAULT 0,
    live_total_stake NUMERIC(12,2) NOT NULL DEFAULT 0,
    live_total_pnl NUMERIC(12,2) NOT NULL DEFAULT 0,
    live_gross_win NUMERIC(12,2) NOT NULL DEFAULT 0,
    live_gross_loss NUMERIC(12,2) NOT NULL DEFAULT 0,
    avg_win NUMERIC(10,4),
    avg_loss NUMERIC(10,4),
    -- Rolling 30d (обновляется cron/hook)
    wr_30d NUMERIC(5,4),
    roi_30d NUMERIC(6,4),
    -- Drawdown tracking
    drawdown_pct NUMERIC(5,4) NOT NULL DEFAULT 0,
    peak_pnl NUMERIC(12,2) NOT NULL DEFAULT 0,
    -- Adaptive sizing output (cache для быстрого чтения из execution_job)
    current_stake NUMERIC(8,2) NOT NULL DEFAULT 5.0,
    kelly_fraction NUMERIC(6,4),
    safety_mult NUMERIC(4,3) NOT NULL DEFAULT 0.250,
    damping NUMERIC(4,3) NOT NULL DEFAULT 0.0,
    -- Pause state
    paused BOOLEAN NOT NULL DEFAULT false,
    paused_reason TEXT,
    paused_at TIMESTAMPTZ,
    -- Bootstrap from dry (чтобы новые live-стратегии стартовали с warm start)
    dry_resolved_at_start INT NOT NULL DEFAULT 0,
    dry_wr_at_start NUMERIC(5,4),
    dry_roi_at_start NUMERIC(6,4),
    -- Housekeeping
    last_settled_at TIMESTAMPTZ,
    last_recomputed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Индекс для быстрого чтения в execution_job (часто вызывается)
CREATE INDEX IF NOT EXISTS idx_slm_paused ON strategy_live_metrics(strategy_key) WHERE paused = false;

-- FK опционально (strategy_state может не иметь строки если новая стратегия создана без dry)
COMMENT ON TABLE strategy_live_metrics IS
    'Adaptive Kelly Sizing — SSoT для live stake/limits каждой стратегии. '
    'Обновляется при settle (hook) и cron live-metrics-recompute (каждые 15 мин).';
