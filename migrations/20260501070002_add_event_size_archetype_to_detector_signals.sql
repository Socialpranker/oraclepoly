-- 20260501070002_add_event_size_archetype_to_detector_signals.sql
-- Description: Денормализованные event_size + archetype в detector_signals — нужны для:
--   - Layer A training (Stage 2): per-event aggregation быстрее
--   - Layer B training (Stage 3): sample_weight = 1/event_size напрямую из row
--   - Honest baseline (Stage 1): per-event Brier через GROUP BY archetype
-- Backfill через scripts/backfill_detector_signals_event_meta.py.

ALTER TABLE public.detector_signals
    ADD COLUMN IF NOT EXISTS event_size integer,
    ADD COLUMN IF NOT EXISTS archetype text
    CHECK (archetype IS NULL OR archetype IN (
        'mutually_exclusive_ladder',
        'one_of_n_choice',
        'independent_multi',
        'hierarchical_time',
        'single_market',
        'unclassified'
    ));

CREATE INDEX IF NOT EXISTS idx_detector_signals_archetype
    ON public.detector_signals (archetype)
    WHERE archetype IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_detector_signals_event_size
    ON public.detector_signals (event_size)
    WHERE event_size IS NOT NULL;
