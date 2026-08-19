-- 20260501070001_add_archetype_to_events.sql
-- Description: Колонка archetype для events — нужна для Stage 2 Layer A (Event Selector).
-- Заполняется backfill-скриптом scripts/backfill_event_archetype.py через research/event_structure_audit.py
-- классификатор. После backfill events sync job обязан проставлять archetype при INSERT/UPDATE.
--
-- Возможные значения (см. memory/event_archetypes_phase1_2026-04-27.md):
--   mutually_exclusive_ladder — числовые диапазоны (Trump posts 100-119, 120-139, ...)
--   one_of_n_choice           — соревнования (Team A vs Team B)
--   independent_multi          — независимые вопросы
--   hierarchical_time          — временные пороги (by April 15? / by April 30?)
--   single_market              — один маркет на event
--   unclassified               — не удалось распознать паттерн (fallback)

ALTER TABLE public.events
    ADD COLUMN IF NOT EXISTS archetype text
    CHECK (archetype IS NULL OR archetype IN (
        'mutually_exclusive_ladder',
        'one_of_n_choice',
        'independent_multi',
        'hierarchical_time',
        'single_market',
        'unclassified'
    ));

CREATE INDEX IF NOT EXISTS idx_events_archetype
    ON public.events (archetype)
    WHERE archetype IS NOT NULL;
