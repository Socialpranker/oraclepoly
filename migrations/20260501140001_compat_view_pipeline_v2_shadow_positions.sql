-- 20260501140001_compat_view_pipeline_v2_shadow_positions.sql
-- Description: Compatibility VIEW для старого UI кода (app/api/bot.py).
--
-- pipeline_v2_shadow_positions была дропнута в Этапе 3, заменена на
-- execution_orders с execution_mode='virtual'. Чтобы не переписывать 9 SQL
-- в bot.py (все used by /v2/* endpoints), создаём VIEW с тем же контрактом.
--
-- Колонки маппинг:
--   id              ← execution_orders.id
--   signal_id       ← execution_orders.detector_signal_id
--   detector        ← execution_orders.detector
--   market_id       ← execution_orders.market_id
--   side            ← execution_orders.side
--   raw_p           — нет (NULL); используется в нескольких UI картах
--   calibrated_p    ← execution_orders.calibrated_p
--   meta_p          ← execution_orders.meta_p
--   entry_price     ← execution_orders.signal_price
--   ev_pct          ← execution_orders.ev_pct
--   stake_usd       ← execution_orders.stake
--   bankroll_at_entry ← execution_orders.bankroll_at_entry
--   resolved        ← (status = 'virtual_settled')
--   realized_pnl_usd ← execution_orders.pnl
--   realized_roi_pct ← (pnl / stake * 100)
--   outcome         ← execution_orders.outcome
--   opened_at       ← execution_orders.created_at
--   resolved_at     ← execution_orders.settled_at

CREATE OR REPLACE VIEW public.pipeline_v2_shadow_positions AS
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
    eo.outcome                                      AS outcome,
    eo.created_at                                   AS opened_at,
    eo.settled_at                                   AS resolved_at
FROM public.execution_orders eo
WHERE eo.execution_mode = 'virtual'
  AND eo.detector IS NOT NULL
  AND eo.detector_signal_id IS NOT NULL
  AND eo.status IN ('virtual_filled', 'virtual_placed', 'virtual_settled');
