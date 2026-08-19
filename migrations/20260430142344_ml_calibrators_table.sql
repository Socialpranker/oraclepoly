-- 20260430142344_ml_calibrators_table.sql
-- Description: Хранилище L1 Beta calibrator parameters per detector.
--
-- Каждое воскресенье ml_calibrator_fit cron берёт rolling 90d resolved
-- detector_signals и фиттит Beta cal (Kull 2017). Параметры пишутся сюда,
-- inference path читает active row для каждого детектора.
--
-- Brier_train / brier_holdout — для мониторинга качества.
-- ADWIN drift detection (Phase E) будет читать holdout метрику.

CREATE TABLE IF NOT EXISTS public.ml_calibrators (
    detector        text         PRIMARY KEY,
    params          jsonb        NOT NULL,
    n_train         integer      NOT NULL,
    n_holdout       integer      NOT NULL,
    brier_raw       numeric(10,6),
    brier_train     numeric(10,6) NOT NULL,
    brier_holdout   numeric(10,6) NOT NULL,
    fit_at          timestamptz  NOT NULL DEFAULT now(),
    created_at      timestamptz  NOT NULL DEFAULT now()
);
