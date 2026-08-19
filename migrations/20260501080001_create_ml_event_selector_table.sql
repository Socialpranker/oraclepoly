-- 20260501080001_create_ml_event_selector_table.sql
-- Description: Хранилище Layer A (Event Selector) моделей.
--
-- Stage 2 / Two-Stage ML Pipeline.
-- Аналогично ml_meta_labeler: version + is_active паттерн для hot-swap.
-- Per-event metrics — Brier считается на агрегированных per-event парах
-- (1 row на event, не на market).

CREATE TABLE IF NOT EXISTS public.ml_event_selector (
    version                  text         PRIMARY KEY,
    model_payload            jsonb        NOT NULL,
    n_events_train           integer      NOT NULL,
    n_events_holdout         integer      NOT NULL,
    train_per_event_brier    numeric(10,6) NOT NULL,
    holdout_per_event_brier  numeric(10,6) NOT NULL,
    -- Раздельные метрики для regime check (HF + Live разделение обязательно)
    hf_holdout_brier         numeric(10,6),
    hf_n_events              integer,
    live_holdout_brier       numeric(10,6),
    live_n_events            integer,
    -- Метаданные
    fit_at                   timestamptz  NOT NULL DEFAULT now(),
    is_active                boolean      NOT NULL DEFAULT false
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ml_event_selector_active
    ON public.ml_event_selector (is_active)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_ml_event_selector_fit_at
    ON public.ml_event_selector (fit_at DESC);
