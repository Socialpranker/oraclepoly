-- 20260505135003_seed_portfolios_and_assign_detectors.sql
-- Description: Seed 5 day-1 portfolios; backfill detector_state.portfolio_id;
-- enforce NOT NULL.
--
-- Mapping: STANDALONE_V2_LGB -> 'ml', everything else -> 'rules'.

INSERT INTO public.portfolios
    (portfolio_id, display_name, bankroll_usd, exposure_cap_usd, kelly_fraction, gate_overrides) VALUES
    ('rules',      'Rules-based',     3000.00, 3000.00, 0.20, '{}'::jsonb),
    ('ml',         'ML model',        1000.00, 1000.00, 0.10, '{"adversarial_mirror":"skip"}'::jsonb),
    ('sentiment',  'Sentiment',        500.00,  500.00, 0.10, '{}'::jsonb),
    ('whale_copy', 'Whale copy',       500.00,  500.00, 0.15, '{"canonical_verdict":"skip"}'::jsonb),
    ('external',   'External signals', 300.00,  300.00, 0.05, '{"days_accumulated":"skip"}'::jsonb)
ON CONFLICT (portfolio_id) DO NOTHING;

UPDATE public.detector_state SET portfolio_id = 'ml'
 WHERE detector = 'STANDALONE_V2_LGB';

UPDATE public.detector_state SET portfolio_id = 'rules'
 WHERE portfolio_id IS NULL;

ALTER TABLE public.detector_state
    ALTER COLUMN portfolio_id SET NOT NULL;
