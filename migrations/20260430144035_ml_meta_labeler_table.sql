-- 20260430144035_ml_meta_labeler_table.sql
-- Description: Хранилище L2 LightGBM meta-labeler models.
--
-- В отличие от ml_calibrators (per-detector), meta-labeler один глобальный.
-- Используем version + is_active паттерн как в ml_models — позволяет hot-swap
-- и rollback при regression.
--
-- model_payload: JSONB с {model_str, feature_names, n_train, brier_*}.
-- LGB booster сериализуется через .model_to_string() — текст ~50KB.

CREATE TABLE IF NOT EXISTS public.ml_meta_labeler (
    version          text         PRIMARY KEY,
    model_payload    jsonb        NOT NULL,
    n_train          integer      NOT NULL,
    n_holdout        integer      NOT NULL,
    train_brier      numeric(10,6) NOT NULL,
    holdout_brier    numeric(10,6) NOT NULL,
    fit_at           timestamptz  NOT NULL DEFAULT now(),
    is_active        boolean      NOT NULL DEFAULT false
);

-- Только одна active модель в каждый момент (для hot-swap)
CREATE UNIQUE INDEX IF NOT EXISTS uq_ml_meta_labeler_active
    ON public.ml_meta_labeler (is_active)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_ml_meta_labeler_fit_at
    ON public.ml_meta_labeler (fit_at DESC);
