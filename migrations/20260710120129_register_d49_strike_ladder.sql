-- 20260710120129_register_d49_strike_ladder.sql
-- Description: D49 StrikeLadder detector registration — shadow-only crypto
-- ladder monotonicity arb (P(YES>K1) >= P(YES>K2) при K1<K2).
--
-- Вердикт: research/d49-strike-ladder-2026-07-09/answer.md (KEEP → shadow).
-- 1.03 net-профитных сигнала/день на живом периоде (2025-01 → 2026-04),
-- медианный net-спред +3.88pp после fee+slippage. Non-overlap с D3 (verified,
-- все крипто strike-рынки имеют neg_risk=false). Требует 4-6 недель
-- forward-накопления перед переводом в virtual/live.

INSERT INTO detector_state (detector, stage, portfolio_id, fractional_kelly_today)
VALUES ('D49_strike_ladder', 'shadow', 'rules', 0.10)
ON CONFLICT (detector) DO UPDATE
  SET stage = EXCLUDED.stage,
      portfolio_id = EXCLUDED.portfolio_id,
      updated_at = now();
