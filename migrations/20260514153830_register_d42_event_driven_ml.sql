-- D42 Event-driven ML detector registration.
--
-- Strategy: ML предсказывает delta_price = yes_price(target_anchor) - yes_price(now).
-- LGBM Regressor per-window (168_to_24, 72_to_24, 24_to_4).
-- Emit signal если |predicted_delta| >= 5pp (cap calibrator weights).
--
-- Source: D3 hypothesis class из prediction_markets_full_landscape.docx
-- Stage: shadow
-- Portfolio: ml (поскольку ML-driven, не rule-based)
-- Fit script: scripts/research/d42_event_driven_ml_fit.py

INSERT INTO detector_state (detector, stage, portfolio_id, fractional_kelly_today)
VALUES ('D42_event_driven_ml', 'shadow', 'ml', 0.10)
ON CONFLICT (detector) DO UPDATE
  SET stage = EXCLUDED.stage,
      portfolio_id = EXCLUDED.portfolio_id,
      updated_at = now();
