-- 20260430164734_pipeline_v2_shadow_positions.sql
-- Description: Paper-trade позиции для Pipeline V2 (D1+D3+D4 → L1+L2 → EV).
--
-- Каждый раз когда pipeline-v2-shadow-executor видит signal с EV>0 → пишет
-- сюда виртуальную позицию. Когда detector_signals.outcome заполняется →
-- считаем realized PnL → используем для UI/drift detection.
--
-- ПОЛНОСТЬЮ независимо от старой strategy_positions — другая бизнес-логика
-- (EV-driven sizing, не divergence threshold).

CREATE TABLE IF NOT EXISTS public.pipeline_v2_shadow_positions (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id       uuid        NOT NULL REFERENCES public.detector_signals(id) ON DELETE CASCADE,
    detector        text        NOT NULL,
    market_id       text        NOT NULL,
    side            text        NOT NULL CHECK (side IN ('YES', 'NO')),

    -- Сигнал на момент открытия
    raw_p           numeric(5,4) NOT NULL,
    calibrated_p    numeric(5,4) NOT NULL,
    meta_p          numeric(5,4) NOT NULL,
    entry_price     numeric(6,4) NOT NULL,

    -- EV-sizer decision
    ev_pct          numeric(7,2) NOT NULL,
    stake_usd       numeric(8,2) NOT NULL,
    bankroll_at_entry numeric(10,2) NOT NULL,

    -- Outcome (заполняется при resolve)
    resolved        boolean     NOT NULL DEFAULT false,
    realized_pnl_usd numeric(10,4),
    realized_roi_pct numeric(8,3),
    outcome         text        CHECK (outcome IS NULL OR outcome IN ('WIN', 'LOSS', 'EXPIRED')),

    -- Timestamps
    opened_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,

    -- Idempotency: один signal → одна shadow позиция
    UNIQUE (signal_id)
);

CREATE INDEX IF NOT EXISTS idx_p2_shadow_opened_at
    ON public.pipeline_v2_shadow_positions (opened_at DESC);

CREATE INDEX IF NOT EXISTS idx_p2_shadow_unresolved
    ON public.pipeline_v2_shadow_positions (resolved, opened_at)
    WHERE resolved = false;

CREATE INDEX IF NOT EXISTS idx_p2_shadow_detector_outcome
    ON public.pipeline_v2_shadow_positions (detector, outcome, opened_at DESC)
    WHERE resolved = true;
