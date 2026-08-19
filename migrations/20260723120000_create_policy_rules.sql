-- 20260723120000_create_policy_rules.sql
-- Description: Phase 2 C0 — версионированная таблица порогов policy engine
-- (design doc docs/superpowers/specs/2026-07-23-portfolio-agent-phase2-policy-engine-design.md §2.7, §7).
-- Каждый DQ-run / proposal ссылается на (rule_key, version) — воспроизводимость вердикта.
-- Правки порогов = новая версия строки (INSERT), НЕ UPDATE params действующей.
-- Seed: пороги DQ-аудита v1, консервативные против прод-baseline 2026-07-23
-- (orphans 0%, no_end_date 5.7%, no_tokens 0.3%, EXPIRED-share 11.1%, freshness — минуты).

CREATE TABLE IF NOT EXISTS policy_rules (
    id          BIGSERIAL PRIMARY KEY,
    rule_key    TEXT NOT NULL,              -- напр. 'dq_audit', 'edge_decay_verdicts' (C1)
    version     INTEGER NOT NULL,
    params      JSONB NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rule_key, version)
);

-- В любой момент максимум одна активная версия правила
CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_rules_active
    ON policy_rules (rule_key) WHERE active;

INSERT INTO policy_rules (rule_key, version, params, active)
VALUES (
    'dq_audit',
    1,
    '{
        "freshness_markets_amber_minutes": 120,
        "freshness_markets_red_minutes": 720,
        "freshness_signals_amber_minutes": 360,
        "freshness_signals_red_minutes": 1440,
        "completeness_end_date_amber_pct": 15.0,
        "completeness_end_date_red_pct": 40.0,
        "completeness_tokens_amber_pct": 5.0,
        "completeness_tokens_red_pct": 20.0,
        "expired_share_amber_pct": 30.0,
        "expired_share_red_pct": 60.0,
        "expired_min_resolved": 50,
        "ri_orphan_amber_pct": 1.0,
        "ri_orphan_red_pct": 5.0,
        "lookback_days": 7
    }'::jsonb,
    true
)
ON CONFLICT (rule_key, version) DO NOTHING;
