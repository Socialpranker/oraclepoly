-- 20260505135002_detector_state_add_portfolio_id.sql
-- Description: Add nullable portfolio_id FK to detector_state.
-- NOT NULL enforced in 20260505135003 after backfill.

ALTER TABLE public.detector_state
    ADD COLUMN IF NOT EXISTS portfolio_id text
        REFERENCES public.portfolios(portfolio_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_detector_state_portfolio
    ON public.detector_state(portfolio_id);
