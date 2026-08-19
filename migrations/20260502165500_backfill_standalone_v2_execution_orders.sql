-- 20260502165500_backfill_standalone_v2_execution_orders.sql
-- Description: Backfill execution_orders из standalone_v2_dry_trades — для появления в Virtual tab.
--
-- Mapping:
--   standalone_v2_dry_trades → execution_orders (execution_mode='virtual', status='virtual_settled')
--
-- /v2/detectors lifecycle.virtual.pnl_usd_7d читает SUM(net_pnl) per detector
-- WHERE execution_mode='virtual' AND created_at >= now() - 7 days.
--
-- См. docs/detector_lifecycle.md (status flow Virtual: virtual_placed → virtual_filled → virtual_settled)

INSERT INTO execution_orders (
    market_id, event_id, side, execution_mode,
    signal_price, order_type, stake, fill_price, filled_qty,
    fee_paid, status, pnl, outcome,
    created_at, placed_at, filled_at, settled_at,
    detector, calibrated_p, meta_p, ev_pct,
    trading_fee_usd, slippage_usd, net_pnl,
    meta
)
SELECT
    dt.condition_id AS market_id,
    dt.event_id,
    dt.side,
    'virtual' AS execution_mode,
    dt.market_yes_price AS signal_price,
    'limit' AS order_type,
    dt.stake_usd AS stake,
    dt.entry_price AS fill_price,
    -- filled_qty: количество shares купленных за stake_usd по entry_price
    ROUND((dt.stake_usd / NULLIF(dt.entry_price, 0))::numeric, 2) AS filled_qty,
    COALESCE(dt.fees_usd, 0) AS fee_paid,
    -- status: virtual_settled если резолвнут, иначе virtual_placed
    CASE WHEN dt.yes_won IS NOT NULL THEN 'virtual_settled' ELSE 'virtual_placed' END AS status,
    dt.pnl_gross AS pnl,
    CASE
        WHEN dt.is_winner IS NULL THEN NULL
        WHEN dt.is_winner THEN 'WIN'
        ELSE 'LOSS'
    END AS outcome,
    dt.decision_at AS created_at,
    dt.decision_at AS placed_at,
    dt.decision_at AS filled_at,  -- virtual fills instant
    dt.resolved_at AS settled_at,
    'STANDALONE_V2_LGB' AS detector,
    dt.predicted_p_yes AS calibrated_p,
    dt.predicted_p_yes AS meta_p,  -- standalone V2 не имеет L2 meta separately
    dt.ev_pct AS ev_pct,
    COALESCE(dt.fees_usd, 0) AS trading_fee_usd,
    -- slippage_usd: разница entry_price vs market_yes_price * stake / market_yes_price
    ROUND((ABS(dt.entry_price - CASE WHEN dt.side='YES' THEN dt.market_yes_price ELSE 1.0 - dt.market_yes_price END) * dt.stake_usd / NULLIF(dt.entry_price, 0))::numeric, 4) AS slippage_usd,
    dt.pnl_net AS net_pnl,
    jsonb_build_object(
        'source', 'standalone_v2_lgb',
        'anchor_hours', dt.anchor_hours,
        'model_version', dt.model_version,
        'predicted_p_yes', dt.predicted_p_yes,
        'category', dt.category,
        'horizon_days', dt.horizon_days
    ) AS meta
FROM standalone_v2_dry_trades dt
WHERE dt.anchor_hours = 24
  AND NOT EXISTS (
      SELECT 1 FROM execution_orders eo
      WHERE eo.execution_mode = 'virtual'
        AND eo.detector = 'STANDALONE_V2_LGB'
        AND eo.market_id = dt.condition_id
        AND eo.created_at = dt.decision_at
  );
