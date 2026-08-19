-- D40 Kalshi cross-platform arbitrage detector registration.
--
-- Strategy: detect price divergences между Polymarket и Kalshi на matched pairs.
-- Phase 1 MVP: shadow only, no auto-execution. Operator closes legs manually.
--
-- Source: research/kalshi-integration-2026-05/prd.md (A1 из prediction_markets_full_landscape.docx)
-- Stage: shadow (no backtest possible — Kalshi не публикует историю)
-- Portfolio: rules (cross-venue arb — rule-based detector, не ML)

INSERT INTO detector_state (detector, stage, portfolio_id, fractional_kelly_today)
VALUES ('D40_kalshi_cross_platform', 'shadow', 'rules', 0.10)
ON CONFLICT (detector) DO UPDATE
  SET stage = EXCLUDED.stage,
      portfolio_id = EXCLUDED.portfolio_id,
      updated_at = now();
