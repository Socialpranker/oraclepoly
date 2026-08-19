-- D41 Momentum detector registration.
--
-- Strategy: rolling 3h price momentum + buy/sell flow alignment → ride trend.
-- Phase 1: shadow only, no canonical backtest (требует backfill через
-- trade_buckets vwap calculation — будет сделан после promote).
--
-- Source: E3 momentum class из prediction_markets_full_landscape.docx
-- Stage: shadow
-- Portfolio: rules (single-market, не ML-driven)

INSERT INTO detector_state (detector, stage, portfolio_id, fractional_kelly_today)
VALUES ('D41_momentum', 'shadow', 'rules', 0.10)
ON CONFLICT (detector) DO UPDATE
  SET stage = EXCLUDED.stage,
      portfolio_id = EXCLUDED.portfolio_id,
      updated_at = now();
