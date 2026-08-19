-- 20260501215910_execution_orders_add_fee_net_pnl.sql
-- Description: Хранить fee/slippage и net PnL в execution_orders для честного дашборда.
--
-- Контекст: settle SQL в v2_execution_resolve пишет `pnl` как gross
-- (stake × payout) без учёта Polymarket fee и market slippage. Особенно
-- критично на extreme prices: NO @ 0.99 даёт gross +1%, но fee + spread
-- съедают всё → реальный net PnL отрицательный.
--
-- Решение: добавить новые колонки `trading_fee_usd`, `slippage_usd`, `net_pnl`.
-- Старая `pnl` остаётся gross (для backwards-compat: ML training на gross,
-- compat VIEW читает gross). Дашборд переключаем на net через COALESCE.
--
-- Compat VIEW pipeline_v2_shadow_positions расширяем 4 новыми колонками:
-- net_pnl_usd, net_roi_pct, trading_fee_usd, slippage_usd.

ALTER TABLE public.execution_orders
    ADD COLUMN IF NOT EXISTS trading_fee_usd numeric(8,4) NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS slippage_usd    numeric(8,4) NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS net_pnl         numeric(10,4);

-- Index для агрегаций в дашборде через WHERE status='virtual_settled'.
CREATE INDEX IF NOT EXISTS idx_execution_orders_settled_net_pnl
    ON public.execution_orders (status, net_pnl)
    WHERE status IN ('virtual_settled', 'settled');

-- Перезаписываем compat VIEW pipeline_v2_shadow_positions добавив net-колонки.
-- DROP+CREATE безопасно — VIEW переcоздаётся атомарно в одной транзакции.
DROP VIEW IF EXISTS public.pipeline_v2_shadow_positions;

CREATE VIEW public.pipeline_v2_shadow_positions AS
SELECT
    eo.id::uuid                                     AS id,
    eo.detector_signal_id                           AS signal_id,
    eo.detector                                     AS detector,
    eo.market_id                                    AS market_id,
    eo.side                                         AS side,
    NULL::numeric(5,4)                              AS raw_p,
    eo.calibrated_p                                 AS calibrated_p,
    eo.meta_p                                       AS meta_p,
    eo.signal_price                                 AS entry_price,
    eo.ev_pct                                       AS ev_pct,
    eo.stake                                        AS stake_usd,
    eo.bankroll_at_entry                            AS bankroll_at_entry,
    (eo.status = 'virtual_settled')                 AS resolved,
    eo.pnl                                          AS realized_pnl_usd,
    CASE
        WHEN eo.status = 'virtual_settled' AND eo.stake > 0
        THEN ROUND((eo.pnl / eo.stake * 100)::numeric, 3)
        ELSE NULL
    END                                             AS realized_roi_pct,
    -- Net PnL колонки (NULL для legacy позиций до миграции)
    eo.net_pnl                                      AS net_pnl_usd,
    CASE
        WHEN eo.status = 'virtual_settled' AND eo.stake > 0 AND eo.net_pnl IS NOT NULL
        THEN ROUND((eo.net_pnl / eo.stake * 100)::numeric, 3)
        ELSE NULL
    END                                             AS net_roi_pct,
    eo.trading_fee_usd                              AS trading_fee_usd,
    eo.slippage_usd                                 AS slippage_usd,
    eo.outcome                                      AS outcome,
    eo.created_at                                   AS opened_at,
    eo.settled_at                                   AS resolved_at
FROM public.execution_orders eo
WHERE eo.execution_mode = 'virtual'
  AND eo.detector IS NOT NULL
  AND eo.detector_signal_id IS NOT NULL
  AND eo.status IN ('virtual_filled', 'virtual_placed', 'virtual_settled');
