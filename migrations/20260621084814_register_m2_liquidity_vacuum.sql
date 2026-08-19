-- M2 Liquidity Vacuum — measurement detector registration (shadow only).
--
-- Measurement instrument (НЕ trading): forward-Δ цены T+30/60м после
-- одностороннего опустошения глубины стакана. Детекция realtime в WSS
-- (app/detectors/m2_vacuum_hook.py), НЕ scan-cron.
-- Истинная метрика = scripts/research/m2_forward_analysis.py (НЕ winner-резолв).
--
-- Спек: docs/superpowers/specs/2026-06-21-m2-liquidity-vacuum-design.md

-- 1. Регистрация в detector_state (shadow).
INSERT INTO detector_state (detector, stage, portfolio_id, fractional_kelly_today)
VALUES ('M2_liquidity_vacuum', 'shadow', 'rules', 0.10)
ON CONFLICT (detector) DO UPDATE
  SET stage = EXCLUDED.stage,
      portfolio_id = EXCLUDED.portfolio_id,
      updated_at = now();

-- 2. Scoped unique index для idempotency (ON CONFLICT DO NOTHING backstop).
--    Существующий uq_detector_signals_d1_tx scoped на D1 — для M2 не сработает,
--    общего unique на (detector, tx_hash) НЕТ. Без своего индекса ON CONFLICT
--    для M2 не имеет конфликтной цели. Realtime-throttle гасит 99%, индекс —
--    backstop на гонки/рестарты.
CREATE UNIQUE INDEX IF NOT EXISTS uq_detector_signals_m2_tx
    ON public.detector_signals (detector, (evidence->>'tx_hash'))
    WHERE detector = 'M2_liquidity_vacuum' AND evidence->>'tx_hash' IS NOT NULL;
