-- 20260501215910_detector_signals_add_source_and_realized_roi_net.sql
-- Description: Разделить gross/net PnL и пометить источник записи (live vs HF backfill).
--
-- Контекст: текущий detector_signals_resolve пишет realized_roi как чистый ROI без
-- учёта Polymarket fee. ML L1/L2 учатся на этом target и получают завышенный edge.
-- Прямая замена сломала бы существующие модели — добавляем `realized_roi_net` рядом.
--
-- source: исторические сигналы из HF backfill (до момента запуска V2 pipeline) —
-- ghost fills + synthetic orderbook → edge завышен на 5-10%. По дате пометим источник
-- чтобы research/L2 могли фильтровать `WHERE source = 'live_v2'`.
--
-- V2 pipeline launched: 2026-04-30 14:36 UTC (см. memory/v2_pipeline_launched_2026-04-30.md).

ALTER TABLE public.detector_signals
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'live_v2'
        CHECK (source IN ('live_v2', 'hf_backfill')),
    ADD COLUMN IF NOT EXISTS realized_roi_net numeric(8,3);

CREATE INDEX IF NOT EXISTS idx_detector_signals_source
    ON public.detector_signals (source);

-- Backfill: всё что сгенерировано до запуска V2 — это HF backfill.
UPDATE public.detector_signals
   SET source = 'hf_backfill'
 WHERE generated_at < '2026-04-30 14:36:00+00'
   AND source = 'live_v2';
