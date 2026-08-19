-- 20260502153706_create_standalone_v2_predictions.sql
-- Description: Shadow run table для standalone LGB V2 model.
--
-- Standalone V2 — это альтернативная ML модель которая работает на ml_features.parquet
-- (per-market, не per-detector-signal). Не заменяет L2 monolith, а run shadow для
-- сравнения per-event Brier 2-4 недели.
--
-- Cron: app/jobs/standalone_v2_predictor.py каждый час сканирует active markets,
-- вычисляет ml_features V2 features as-of now, прогоняет через standalone модель,
-- записывает predicted_p_yes здесь. Когда market резолвится — backfill outcome.
--
-- Метрика для cutover: per-event Brier на резолвнувшихся predictions.
--
-- Production не трогаем (active L2 = lgb_l2_v20260502_053135 остаётся).

CREATE TABLE IF NOT EXISTS public.standalone_v2_predictions (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    event_id        TEXT,
    anchor_hours    INTEGER NOT NULL,         -- 4, 24, 72, 168 (skip 720 — overfit risk)
    model_version   TEXT NOT NULL,            -- standalone_lgb_v2_anchor24h_20260502_121953
    predicted_p_yes DOUBLE PRECISION NOT NULL CHECK (predicted_p_yes BETWEEN 0 AND 1),
    market_yes_price DOUBLE PRECISION,        -- snapshot YES price на момент prediction
    ev_yes_pct      DOUBLE PRECISION,         -- (predicted_p_yes - market_yes_price) / market_yes_price * 100
    ev_no_pct       DOUBLE PRECISION,         -- ((1-predicted_p_yes) - (1-market_yes_price)) / (1-market_yes_price) * 100
    category        TEXT,
    horizon_days    DOUBLE PRECISION,
    n_features_used INTEGER,
    inference_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- backfilled when market resolves
    yes_won         INTEGER CHECK (yes_won IN (0, 1)),
    resolved_at     TIMESTAMPTZ,
    brier_squared   DOUBLE PRECISION          -- (predicted_p_yes - yes_won)^2
);

CREATE INDEX IF NOT EXISTS idx_standalone_v2_predictions_market_anchor
    ON public.standalone_v2_predictions (market_id, anchor_hours);
CREATE INDEX IF NOT EXISTS idx_standalone_v2_predictions_inference_at
    ON public.standalone_v2_predictions (inference_at DESC);
CREATE INDEX IF NOT EXISTS idx_standalone_v2_predictions_unresolved
    ON public.standalone_v2_predictions (resolved_at) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_standalone_v2_predictions_event_id
    ON public.standalone_v2_predictions (event_id) WHERE event_id IS NOT NULL;

-- Уникальный prediction per (market, anchor, model_version) — одна prediction в час
CREATE UNIQUE INDEX IF NOT EXISTS uq_standalone_v2_predictions_market_anchor_version_hour
    ON public.standalone_v2_predictions (market_id, anchor_hours, model_version, DATE_TRUNC('hour', inference_at));

COMMENT ON TABLE public.standalone_v2_predictions IS
    'Shadow run results для standalone LGB V2 model (alternative L2). Cron каждый час, backfill outcome при резолве.';
