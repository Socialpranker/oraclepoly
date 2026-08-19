-- 20260703160000_gate_shadow_log.sql
-- Shadow-телеметрия для гейтов на пересмотре — фиксирует книгу ордеров на момент
-- reject'а, чтобы позже честно посчитать "would have filled" PnL (join к
-- detector_signals.outcome/realized_roi_net по detector_signal_id), не по
-- signal-цене как в первом проходе аудита. Первый потребитель:
-- category_blacklist:Sports на D3_cross_mkt_arb (research/fable-gates-audit-2026-07-03/,
-- план §B). Гейт введён 2026-05-18 (bfea429f) на направленных детекторах
-- (Sports -$5,818/14d); у D3 (структурный арбитраж) counterfactual отбитых
-- сигналов иной (61 WIN / 3 LOSS за 30д, evidence/06). Цель: 2-3 недели данных,
-- затем human decision об исключении D3 из _CATEGORY_BLACKLIST (n>=30 resolved, ROI>0).
-- Retention: не задан явно (короткий эксперимент, ручная чистка после решения).
CREATE TABLE IF NOT EXISTS gate_shadow_log (
    id                bigserial PRIMARY KEY,
    logged_at         timestamptz NOT NULL DEFAULT now(),
    detector          text        NOT NULL,
    gate              text        NOT NULL,   -- 'category_blacklist:Sports' etc.
    detector_signal_id uuid,
    stake_usd         numeric(10,2),          -- NULL если reject случился до sizing
    best_bid          numeric(8,6),
    best_ask          numeric(8,6),
    depth_usd         numeric(12,2)           -- best_bid_size + best_ask_size, в $ по best price
);

CREATE INDEX IF NOT EXISTS idx_gsl_detector_gate_time ON gate_shadow_log (detector, gate, logged_at);
