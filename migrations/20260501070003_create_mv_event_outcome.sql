-- 20260501070003_create_mv_event_outcome.sql
-- Description: Materialized view с per-event агрегациями для Layer A (Stage 2) ground truth.
--
-- Источник: detector_signals + pipeline_v2_shadow_positions
-- Семантика "перспективности event'а" (Layer A target):
--   event_roi = SUM(realized_pnl_usd) / SUM(stake_usd)  (stake-weighted ROI)
-- Если pipeline_v2_shadow_positions пуст для event'а — fallback к mean(realized_roi)
-- из detector_signals.
--
-- Refresh: hourly cron `refresh-mv-event-outcome` (см. scripts/cron_runner.py).
-- CONCURRENTLY safe — есть UNIQUE INDEX на event_id.

CREATE MATERIALIZED VIEW IF NOT EXISTS public.mv_event_outcome AS
SELECT
    ds.event_id                                              AS event_id,
    COUNT(*)                                                 AS n_signals,
    COUNT(DISTINCT ds.market_id)                             AS n_markets,
    AVG(ds.realized_roi)                                     AS mean_realized_roi,
    SUM(CASE WHEN ds.outcome = 'WIN'     THEN 1 ELSE 0 END)  AS n_wins,
    SUM(CASE WHEN ds.outcome = 'LOSS'    THEN 1 ELSE 0 END)  AS n_losses,
    SUM(CASE WHEN ds.outcome = 'EXPIRED' THEN 1 ELSE 0 END)  AS n_expired,
    -- Stake-weighted PnL из shadow positions (предпочтительный target)
    SUM(p2.realized_pnl_usd)                                 AS sum_pnl_usd,
    SUM(p2.stake_usd)                                        AS sum_stake_usd,
    CASE
        WHEN COALESCE(SUM(p2.stake_usd), 0) > 0
        THEN SUM(p2.realized_pnl_usd) / NULLIF(SUM(p2.stake_usd), 0)
        ELSE AVG(ds.realized_roi)  -- fallback
    END                                                      AS event_roi,
    MAX(ds.archetype)                                        AS archetype,  -- одинаковый для всех signals event'а
    MIN(ds.generated_at)                                     AS first_signal_at,
    MAX(ds.resolved_at)                                      AS last_resolved_at
FROM public.detector_signals ds
LEFT JOIN public.pipeline_v2_shadow_positions p2
    ON p2.signal_id = ds.id AND p2.resolved = true
WHERE ds.resolved = true
  AND ds.outcome IN ('WIN', 'LOSS', 'EXPIRED')
  AND ds.event_id IS NOT NULL
GROUP BY ds.event_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_event_outcome_event_id
    ON public.mv_event_outcome (event_id);

CREATE INDEX IF NOT EXISTS idx_mv_event_outcome_event_roi
    ON public.mv_event_outcome (event_roi);
