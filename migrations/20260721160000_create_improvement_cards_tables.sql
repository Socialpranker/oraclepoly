-- 20260721160000_create_improvement_cards_tables.sql
-- Description: Improvement Cards (Phase 1, Компонент 4 Portfolio Analyst Agent).
-- Очередь предложений улучшений из выходов аудитов, ранжированных по EV/час.
-- Design doc: docs/superpowers/specs/2026-07-21-improvement-cards-design.md.
-- agent_runs.kind='monitoring' уже в CHECK (миграция 20260721120000) — НЕ расширяем.

CREATE TABLE IF NOT EXISTS improvement_cards (
    id                BIGSERIAL PRIMARY KEY,
    card_key          TEXT NOT NULL,          -- UNIQUE дедуп: "{source_kind}:{scope}"
    source_kind       TEXT NOT NULL,          -- adverse_selection | edge_decay | counterfactual
    scope             TEXT NOT NULL,          -- унаследован от claim: "detector:D36_obi_pressure"
    title             TEXT NOT NULL,

    claim_id          BIGINT NOT NULL REFERENCES agent_claims (id),
    evidence_run_ids  BIGINT[] NOT NULL,
    mechanism_ref     TEXT,

    leak_usd          NUMERIC(14, 2),
    p_fix_success     NUMERIC(4, 3),
    effort_hours      NUMERIC(6, 2),
    ev_per_hour       NUMERIC(14, 2) GENERATED ALWAYS AS (
                          CASE WHEN effort_hours > 0 AND leak_usd IS NOT NULL
                               THEN ABS(leak_usd) * p_fix_success / effort_hours END
                      ) STORED,

    lifecycle_stage   TEXT NOT NULL DEFAULT 'shadow'
                          CHECK (lifecycle_stage IN ('shadow', 'digest', 'actionable')),
    attention_status  TEXT NOT NULL DEFAULT 'pending'
                          CHECK (attention_status IN ('pending', 'acted', 'acknowledged', 'ignored')),
    status            TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open', 'accepted', 'deferred', 'rejected', 'resolved', 'stale')),

    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_improvement_cards_card_key ON improvement_cards (card_key);
CREATE INDEX IF NOT EXISTS idx_improvement_cards_status ON improvement_cards (status);
CREATE INDEX IF NOT EXISTS idx_improvement_cards_ev ON improvement_cards (ev_per_hour DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS improvement_cards_config (
    id                 INTEGER PRIMARY KEY DEFAULT 1,
    tiers              JSONB NOT NULL,
    ignored_after_days INTEGER NOT NULL DEFAULT 30,
    stale_after_days   INTEGER NOT NULL DEFAULT 90,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO improvement_cards_config (id, tiers)
VALUES (
    1,
    '{"adverse_selection": {"p_fix": 0.6, "hours": 2},
      "edge_decay": {"p_fix": 0.4, "hours": 4},
      "counterfactual": {"p_fix": 0.5, "hours": 3}}'::jsonb
)
ON CONFLICT (id) DO NOTHING;
