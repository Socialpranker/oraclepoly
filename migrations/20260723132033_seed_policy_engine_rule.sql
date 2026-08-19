-- Phase 2 C1: seed порогов policy engine v1 (rule_key='policy_engine').
-- Пороги live drawdown — решение владельца 2026-07-23 (прод-baseline D3: ROI −33%/30d):
--   REDUCE_RISK_REVIEW при bankroll-weighted net ROI 30d < −15%,
--   CLOSE_POSITION_REVIEW при < −30%, минимум 20 settled live-ордеров.
-- Правки порогов = INSERT новой версии + деактивация старой, НЕ UPDATE params
-- (частичный уникальный индекс uq_policy_rules_active гарантирует одну активную версию).

INSERT INTO policy_rules (rule_key, version, params, active)
VALUES (
    'policy_engine',
    1,
    '{
        "drawdown_reduce_roi": -0.15,
        "drawdown_close_roi": -0.30,
        "drawdown_min_settled": 20,
        "drawdown_window_days": 30,
        "cooldown_hours": 6
    }'::jsonb,
    true
)
ON CONFLICT (rule_key, version) DO NOTHING;
