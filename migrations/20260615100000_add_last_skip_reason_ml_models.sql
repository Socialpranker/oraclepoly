-- Add last_skip_reason to ML model tables for observability of "frozen by gate" state.
--
-- Контекст: L2 meta_labeler (0 active) и Layer A event_selector (замёрз 14 мая)
-- НЕ сломаны — validation-gate честно блокирует новые модели из-за нестационарности
-- рынка. Но в БД это выглядит как голый is_active=false без причины → неотличимо от
-- сбоя. Колонка хранит человекочитаемую причину последнего НЕ-промоута (reason из
-- _persist_and_maybe_promote), чтобы дашборд/мониторинг отличали frozen-by-gate от
-- упавшего джоба. См. memory ml_gate_live_holdout_recency / strategy_edge_not_data_2026-06-15.

ALTER TABLE public.ml_event_selector
    ADD COLUMN IF NOT EXISTS last_skip_reason text;

ALTER TABLE public.ml_meta_labeler
    ADD COLUMN IF NOT EXISTS last_skip_reason text;

ALTER TABLE public.ml_market_picker
    ADD COLUMN IF NOT EXISTS last_skip_reason text;

COMMENT ON COLUMN public.ml_event_selector.last_skip_reason IS
    'Причина последнего НЕ-промоута этой версии (VALIDATION_GATE_FAILED / live-ratio / ...). '
    'NULL если версия промоутнута. Для наблюдаемости frozen-by-gate vs broken.';

COMMENT ON COLUMN public.ml_meta_labeler.last_skip_reason IS
    'Причина последнего НЕ-промоута этой версии. NULL если промоутнута. '
    'Для наблюдаемости frozen-by-gate vs broken.';

COMMENT ON COLUMN public.ml_market_picker.last_skip_reason IS
    'Причина последнего НЕ-промоута этой версии. NULL если промоутнута. '
    'Для наблюдаемости frozen-by-gate vs broken.';
