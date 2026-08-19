-- 20260501080003_create_pipeline_v2_shadow_predictions.sql
-- Description: Параллельные предсказания monolith vs v2_two_stage на каждый detector_signal.
--
-- Stage 5 / Two-Stage ML Pipeline.
-- Используется для решения о cutover (Stage 6). НЕ для production trading
-- (это observability/decision-support, не state).
--
-- Один row на detector_signal_id — каждый signal пишется один раз сразу после
-- генерации. Outcome заполняется когда detector_signals.outcome заполняется
-- (resolve cron). Per-event Brier comparison считается отдельным cron'ом.

CREATE TABLE IF NOT EXISTS public.pipeline_v2_shadow_predictions (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    detector_signal_id    uuid        NOT NULL REFERENCES public.detector_signals(id) ON DELETE CASCADE,
    detector              text        NOT NULL,
    market_id             text        NOT NULL,
    event_id              text,
    side                  text        NOT NULL,
    -- Monolith prediction (legacy L1+L2)
    monolith_calibrated_p numeric(5,4) NOT NULL,
    monolith_meta_p       numeric(5,4) NOT NULL,
    -- V2 prediction (Layer A + Layer B)
    v2_calibrated_p       numeric(5,4) NOT NULL,
    v2_p_event            numeric(5,4),
    v2_passed_gate        boolean,
    v2_meta_p             numeric(5,4) NOT NULL,
    -- Active model versions для воспроизводимости (audit trail)
    monolith_version      text,
    layer_a_version       text,
    layer_b_version       text,
    -- Outcome (заполняется при resolve detector_signals)
    resolved              boolean     NOT NULL DEFAULT false,
    outcome_int           integer,    -- 1=WIN, 0=LOSS, NULL=EXPIRED
    -- Timestamps
    predicted_at          timestamptz NOT NULL DEFAULT now(),
    resolved_at           timestamptz,
    -- Idempotency: один signal → одна shadow prediction
    UNIQUE (detector_signal_id)
);

CREATE INDEX IF NOT EXISTS idx_p2_shadow_pred_predicted_at
    ON public.pipeline_v2_shadow_predictions (predicted_at DESC);

CREATE INDEX IF NOT EXISTS idx_p2_shadow_pred_event_resolved
    ON public.pipeline_v2_shadow_predictions (event_id, resolved)
    WHERE resolved = true;
