-- D36 OBI Pressure detector registration.
--
-- Strategy: Orderbook Imbalance signal в mid-zone markets [0.30-0.85].
-- Source: research/polymarket-orthogonal-strategies/report.md (2026-03-26)
--
-- Stage: shadow (initial accumulation, нет backtest на нашей платформе).
-- Promote to virtual после ~30+ resolved signals с WR >= 55%.

INSERT INTO detector_state (detector, stage, portfolio_id, fractional_kelly_today)
VALUES ('D36_obi_pressure', 'shadow', 'rules', 0.10)
ON CONFLICT (detector) DO UPDATE
  SET stage = EXCLUDED.stage,
      portfolio_id = EXCLUDED.portfolio_id,
      updated_at = now();
