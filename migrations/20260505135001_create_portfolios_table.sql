-- 20260505135001_create_portfolios_table.sql
-- Description: Portfolios as first-class entities. 5 buckets day-1
-- (rules, ml, sentiment, whale_copy, external). Each owns its own
-- bankroll, exposure cap, kelly fraction, and gate overrides.
--
-- Phase 1 of plan: bright-hopping-meteor.md (portfolios refactor).
-- Backwards-compatible: no app code reads this table yet.

CREATE TABLE IF NOT EXISTS public.portfolios (
    portfolio_id          text PRIMARY KEY,
    display_name          text NOT NULL,
    bankroll_usd          numeric(10,2) NOT NULL,
    exposure_cap_usd      numeric(10,2) NOT NULL,
    kelly_fraction        numeric(4,3) NOT NULL DEFAULT 0.10,
    gate_overrides        jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled               boolean NOT NULL DEFAULT true,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN public.portfolios.gate_overrides IS
'JSONB map: gate_name -> action. Recognised: "skip" (treat as passed), "relax" (lower threshold). Unknown keys ignored. v1 gates: adversarial_mirror, days_accumulated, canonical_verdict.';
