-- 20260501080002_create_ml_market_picker_table.sql
-- Description: Хранилище Layer B (Market+Side Picker) моделей.
--
-- Stage 3 / Two-Stage ML Pipeline.
-- Per-row model (1 row = 1 detector signal), но GroupKFold по event_id +
-- sample_weight = 1/event_size для устранения per-event correlation.
-- Per-category breakdown в JSONB — нужен для promotion gate
-- (отсев Tech&AI/World&Economy если < 0.25 brier и n_events ≥ 10).

CREATE TABLE IF NOT EXISTS public.ml_market_picker (
    version                  text         PRIMARY KEY,
    model_payload            jsonb        NOT NULL,
    n_train_rows             integer      NOT NULL,
    n_holdout_rows           integer      NOT NULL,
    n_events_train           integer      NOT NULL,
    n_events_holdout         integer      NOT NULL,
    train_per_event_brier    numeric(10,6) NOT NULL,
    holdout_per_event_brier  numeric(10,6) NOT NULL,
    -- HF / Live разделение (обязательно для Stage 3 promotion gate)
    hf_holdout_brier         numeric(10,6),
    hf_n_events              integer,
    live_holdout_brier       numeric(10,6),
    live_n_events            integer,
    -- Per-category breakdown: {category: {brier, n_events, win_rate}}
    per_category_metrics     jsonb,
    -- Top-1 per-event accuracy (selection power: model выбрал winning market в event'е?)
    top1_per_event_accuracy  numeric(5,4),
    -- Layer A threshold для воспроизводимости universe
    layer_a_threshold        numeric(4,3),
    -- Метаданные
    fit_at                   timestamptz  NOT NULL DEFAULT now(),
    is_active                boolean      NOT NULL DEFAULT false
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ml_market_picker_active
    ON public.ml_market_picker (is_active)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_ml_market_picker_fit_at
    ON public.ml_market_picker (fit_at DESC);
