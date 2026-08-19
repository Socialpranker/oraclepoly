-- 20260502163421_register_standalone_v2_lgb_detector.sql
-- Description: Регистрирует STANDALONE_V2_LGB как pseudo-detector в 4-stage architecture.
--
-- Standalone V2 LGB — model-based decision (не pattern-match как D1-D6). Эмитит
-- signals для ВСЕХ active markets per anchor. Чтобы появилось в /v2/detectors UI
-- рядом с D1-D6, мапим:
--   standalone_v2_predictions → detector_signals (detector='STANDALONE_V2_LGB', source='hf_backfill')
--   standalone_v2_dry_trades  → execution_orders (когда реализуется live integration)
--
-- См. docs/backtests/STANDALONE_V2_LGB.md (PASS, $32.01/d honest@$5K, ROI 31%)
-- См. docs/detector_lifecycle.md (общие правила 4-stage)

-- Step 1: Register в detector_state
INSERT INTO detector_state (detector, stage, n_resolved, fractional_kelly_today)
VALUES ('STANDALONE_V2_LGB', 'shadow', 0, 0.10)
ON CONFLICT (detector) DO NOTHING;

-- Step 2: Backfill detector_signals из standalone_v2_predictions
-- (только anchor=24h — production target; остальные anchors остаются в predictions table для исследования)
INSERT INTO detector_signals (
    detector, market_id, event_id, side,
    confidence, expected_edge_pct, fair_price, current_price,
    horizon_sec, evidence, generated_at,
    resolved, outcome, realized_roi, realized_roi_net, resolved_at,
    source
)
SELECT
    'STANDALONE_V2_LGB' AS detector,
    p.condition_id AS market_id,
    p.event_id,
    -- side: выбираем по большему EV
    CASE
        WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES'
        ELSE 'NO'
    END AS side,
    -- confidence = вероятность на выбранную side
    CASE
        WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN p.predicted_p_yes
        ELSE 1.0 - p.predicted_p_yes
    END AS confidence,
    -- expected_edge_pct = max EV
    GREATEST(COALESCE(p.ev_yes_pct, 0), COALESCE(p.ev_no_pct, 0)) AS expected_edge_pct,
    -- fair_price = модельная вероятность YES (не зависит от side)
    p.predicted_p_yes AS fair_price,
    -- current_price = рыночная YES price
    p.market_yes_price AS current_price,
    -- horizon_sec = anchor_hours * 3600
    p.anchor_hours * 3600 AS horizon_sec,
    -- evidence: дополнительная metadata
    jsonb_build_object(
        'anchor_hours', p.anchor_hours,
        'model_version', p.model_version,
        'predicted_p_yes', p.predicted_p_yes,
        'ev_yes_pct', p.ev_yes_pct,
        'ev_no_pct', p.ev_no_pct,
        'category', p.category
    ) AS evidence,
    p.inference_at AS generated_at,
    -- Outcome (если резолвнут)
    p.yes_won IS NOT NULL AS resolved,
    CASE
        WHEN p.yes_won IS NULL THEN NULL
        WHEN (
            (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'YES'
            AND p.yes_won = 1
        ) OR (
            (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'NO'
            AND p.yes_won = 0
        ) THEN 'WIN'
        ELSE 'LOSS'
    END AS outcome,
    -- realized_roi: side-aware payout (no fees)
    CASE
        WHEN p.yes_won IS NULL OR p.market_yes_price IS NULL THEN NULL
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'YES'
            AND p.yes_won = 1
            AND p.market_yes_price > 0
            THEN ROUND(((1.0 / p.market_yes_price - 1.0) * 100)::numeric, 3)
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'NO'
            AND p.yes_won = 0
            AND p.market_yes_price < 1
            THEN ROUND(((1.0 / (1.0 - p.market_yes_price) - 1.0) * 100)::numeric, 3)
        ELSE -100.0
    END AS realized_roi,
    -- realized_roi_net: со slippage 1% + fees 2% applied
    CASE
        WHEN p.yes_won IS NULL OR p.market_yes_price IS NULL THEN NULL
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'YES'
            AND p.yes_won = 1
            AND p.market_yes_price > 0
            THEN ROUND((((1.0 / (p.market_yes_price * 1.01) - 1.0) * 100) - 4.0)::numeric, 3)
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'NO'
            AND p.yes_won = 0
            AND p.market_yes_price < 1
            THEN ROUND((((1.0 / ((1.0 - p.market_yes_price) * 1.01) - 1.0) * 100) - 4.0)::numeric, 3)
        ELSE -102.0
    END AS realized_roi_net,
    p.resolved_at,
    'hf_backfill' AS source
FROM standalone_v2_predictions p
WHERE p.anchor_hours = 24
  -- только signals прошедшие EV gate: положительный edge для chosen side (consistent с dry_runner)
  -- Negative EV = "плохая ставка" не должна попадать в shadow
  AND (COALESCE(p.ev_yes_pct, 0) > 10 OR COALESCE(p.ev_no_pct, 0) > 10)
  -- price diapason (anti thin/edge markets)
  AND p.market_yes_price BETWEEN 0.05 AND 0.95
  -- не дублировать (если уже есть)
  AND NOT EXISTS (
      SELECT 1 FROM detector_signals ds
      WHERE ds.detector = 'STANDALONE_V2_LGB'
        AND ds.market_id = p.condition_id
        AND ds.generated_at = p.inference_at
  );

-- Update detector_state.n_resolved
UPDATE detector_state
SET n_resolved = (
    SELECT COUNT(*) FROM detector_signals
    WHERE detector = 'STANDALONE_V2_LGB' AND resolved = true
)
WHERE detector = 'STANDALONE_V2_LGB';

-- source field: 'live_v2' = live D1-D6 detector emit, 'hf_backfill' = retrospective backfill (HF parquet или standalone_v2_predictions canonical)
