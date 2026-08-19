-- D43 ResolutionDispute detector registration.
--
-- Strategy: long-stuck markets (24h-168h overdue) с extreme zone consensus.
-- Fade consensus как hint что operator imag review UMA dispute outcome.
-- SHADOW only — emit signals, operator decides per-signal.
--
-- Source: G3 hypothesis из prediction_markets_full_landscape.docx
-- Stage: shadow
-- Portfolio: rules

INSERT INTO detector_state (detector, stage, portfolio_id, fractional_kelly_today)
VALUES ('D43_resolution_dispute', 'shadow', 'rules', 0.05)
ON CONFLICT (detector) DO UPDATE
  SET stage = EXCLUDED.stage,
      portfolio_id = EXCLUDED.portfolio_id,
      updated_at = now();
