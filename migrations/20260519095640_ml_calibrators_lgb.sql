-- 20260519095640_ml_calibrators_lgb.sql
-- Description: Per-detector LightGBM calibrators (parallel to ml_calibrators Beta).
--
-- Контекст 2026-05-19 pilot: некоторые детекторы (D31, D41, D33) выдают constant
-- raw confidence — Beta calibrator не может улучшить (2 параметра, 1 input feature).
-- LightGBM использует full feature set (zone, category, edge_pct, ...) и
-- различает signals. D41 pilot: LGB Brier 0.225 vs Beta 0.249 (+9.5%), AUC 0.677.
--
-- Жизненный цикл:
--   1. Inference: detector_signal_service пытается LGB → Beta → raw_p (fallback chain)
--   2. Fit job: app/jobs/ml_calibrator_lgb_fit.py запускается еженедельно
--   3. Per-detector один active row (UNIQUE WHERE is_active = true)

CREATE TABLE IF NOT EXISTS public.ml_calibrators_lgb (
    version          text NOT NULL PRIMARY KEY,
    detector         text NOT NULL,
    -- Сериализованный LightGBM booster + feature_names + metadata
    model_payload    jsonb NOT NULL,
    -- Train metadata
    n_train          integer NOT NULL,
    n_holdout        integer NOT NULL,
    train_brier      numeric(10,6) NOT NULL,
    holdout_brier    numeric(10,6) NOT NULL,
    holdout_auc      numeric(6,4),
    holdout_ece      numeric(10,6),
    -- Baseline сравнения (на том же holdout):
    --   brier_raw_p   — raw_p без любой калибровки
    --   brier_beta    — Beta calibrator (если был fit'ed на том же train)
    -- Это позволяет honest сравнение для promote-решения.
    brier_raw_p      numeric(10,6),
    brier_beta       numeric(10,6),
    fit_at           timestamptz NOT NULL DEFAULT now(),
    is_active        boolean NOT NULL DEFAULT false
);

-- Только один active per detector. Indexes:
CREATE UNIQUE INDEX IF NOT EXISTS uq_ml_calibrators_lgb_active_per_detector
    ON public.ml_calibrators_lgb (detector)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_ml_calibrators_lgb_detector
    ON public.ml_calibrators_lgb (detector, fit_at DESC);

CREATE INDEX IF NOT EXISTS idx_ml_calibrators_lgb_fit_at
    ON public.ml_calibrators_lgb (fit_at DESC);

COMMENT ON TABLE public.ml_calibrators_lgb IS
    'Per-detector LightGBM calibrators. Используется для детекторов с constant raw_p или сложным feature interaction. Inference fallback chain: LGB → Beta (ml_calibrators) → raw_p.';
