-- 20260501130002_recreate_mv_event_outcome_v2.sql
-- Description: Пересоздать mv_event_outcome поверх execution_orders (status='settled')
-- вместо удалённой pipeline_v2_shadow_positions.
--
-- Источник: detector_signals + execution_orders (V2 ledger)
-- Семантика "перспективности event'а" (Layer A target):
--   event_roi = SUM(pnl) / SUM(stake)  (stake-weighted ROI)
-- Если execution_orders пуст для event'а — fallback к mean(realized_roi)
-- из detector_signals.

DROP MATERIALIZED VIEW IF EXISTS public.mv_event_outcome CASCADE;

CREATE MATERIALIZED VIEW public.mv_event_outcome AS
SELECT
    ds.event_id                                              AS event_id,
    COUNT(*)                                                 AS n_signals,
    COUNT(DISTINCT ds.market_id)                             AS n_markets,
    AVG(ds.realized_roi)                                     AS mean_realized_roi,
    SUM(CASE WHEN ds.outcome = 'WIN'     THEN 1 ELSE 0 END)  AS n_wins,
    SUM(CASE WHEN ds.outcome = 'LOSS'    THEN 1 ELSE 0 END)  AS n_losses,
    SUM(CASE WHEN ds.outcome = 'EXPIRED' THEN 1 ELSE 0 END)  AS n_expired,
    SUM(eo.pnl)                                              AS sum_pnl_usd,
    SUM(eo.stake) FILTER (WHERE eo.status = 'settled')       AS sum_stake_usd,
    CASE
        WHEN COALESCE(SUM(eo.stake) FILTER (WHERE eo.status = 'settled'), 0) > 0
        THEN SUM(eo.pnl) / NULLIF(SUM(eo.stake) FILTER (WHERE eo.status = 'settled'), 0)
        ELSE AVG(ds.realized_roi)
    END                                                      AS event_roi,
    MAX(ds.archetype)                                        AS archetype,
    MIN(ds.generated_at)                                     AS first_signal_at,
    MAX(ds.resolved_at)                                      AS last_resolved_at
FROM public.detector_signals ds
LEFT JOIN public.execution_orders eo
    ON eo.detector_signal_id = ds.id AND eo.status = 'settled'
WHERE ds.resolved = true
  AND ds.outcome IN ('WIN', 'LOSS', 'EXPIRED')
  AND ds.event_id IS NOT NULL
GROUP BY ds.event_id;

CREATE UNIQUE INDEX uq_mv_event_outcome_event_id
    ON public.mv_event_outcome (event_id);

CREATE INDEX idx_mv_event_outcome_event_roi
    ON public.mv_event_outcome (event_roi);
