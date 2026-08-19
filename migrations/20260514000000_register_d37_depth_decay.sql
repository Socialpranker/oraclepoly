-- D37 Depth Decay detector registration.
--
-- Strategy: passive maker on widened spreads near resolution.
-- Source: H1 hypothesis в research/polymarket-alpha-hypotheses-2026-05/2026-05-12.md
--
-- Stage: shadow (initial accumulation, нет backtest на нашей платформе).
-- Run in same scan as D36 (shared CLOB book fetch).

INSERT INTO detector_state (detector, stage, portfolio_id, fractional_kelly_today)
VALUES ('D37_depth_decay', 'shadow', 'rules', 0.10)
ON CONFLICT (detector) DO UPDATE
  SET stage = EXCLUDED.stage,
      portfolio_id = EXCLUDED.portfolio_id,
      updated_at = now();
