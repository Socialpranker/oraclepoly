-- 141: Таблица calibration_configs для динамических весов Bayesian Oracle
-- Позволяет хранить и переключать конфигурации весов без деплоя

CREATE TABLE IF NOT EXISTS public.calibration_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category TEXT NOT NULL DEFAULT 'global',
    w_mkt NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    w_whale NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    w_news NUMERIC(6,4) NOT NULL DEFAULT 0.8,
    temperature NUMERIC(6,4) NOT NULL DEFAULT 1.0,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    source TEXT NOT NULL DEFAULT 'manual',
    brier_score_before NUMERIC(8,6),
    brier_score_after NUMERIC(8,6),
    log_loss_before NUMERIC(8,6),
    log_loss_after NUMERIC(8,6),
    sample_count INTEGER DEFAULT 0,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Гарантия: максимум один активный конфиг на категорию
CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_configs_active_category
    ON public.calibration_configs (category) WHERE is_active = TRUE;

-- Seed дефолтных весов (global)
INSERT INTO public.calibration_configs (category, w_mkt, w_whale, w_news, temperature, is_active, source)
VALUES ('global', 1.0, 1.0, 0.8, 1.0, TRUE, 'seed')
ON CONFLICT DO NOTHING;
