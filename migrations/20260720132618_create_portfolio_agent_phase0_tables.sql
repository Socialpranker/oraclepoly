-- 20260720132618_create_portfolio_agent_phase0_tables.sql
-- Description: Portfolio Analyst Agent Phase 0 — 4 ledger-таблицы (claims/runs/actions/llm_calls).
-- Торговые таблицы не трогаются, только read-only роль к ним (см. отдельную задачу
-- "read-only DB-роль" в docs/superpowers/plans/2026-07-20-portfolio-agent-phase0.md).
-- Спека: docs/specs/portfolio_analyst_agent.md §5.

-- === agent_claims — реестр утверждений (§3.1) ===
CREATE TABLE IF NOT EXISTS agent_claims (
    id              BIGSERIAL PRIMARY KEY,
    statement       TEXT NOT NULL,
    scope           TEXT NOT NULL,  -- категория/детектор/площадка/политика, свободный текст с конвенцией "type:value" (напр. "detector:d42", "policy:exit_strategy")
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'expired', 'contradicted', 'superseded')),
    evidence_run_ids BIGINT[] NOT NULL DEFAULT '{}',  -- ссылки на agent_runs.id, на чём основан клейм
    last_verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    verify_policy   TEXT NOT NULL DEFAULT 'time_halflife'
                        CHECK (verify_policy IN ('time_halflife', 'event_trigger')),
    verify_halflife_days INTEGER,          -- NULL допустим только для verify_policy='event_trigger'
    depends_on      BIGINT[] NOT NULL DEFAULT '{}',   -- id других agent_claims, от которых зависит этот клейм
    dependents      BIGINT[] NOT NULL DEFAULT '{}',   -- обратный граф (денормализация для быстрого чтения; источник правды — depends_on)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_claims_status ON agent_claims (status);
CREATE INDEX IF NOT EXISTS idx_agent_claims_scope ON agent_claims (scope);
CREATE INDEX IF NOT EXISTS idx_agent_claims_last_verified_at ON agent_claims (last_verified_at);
-- UNIQUE на statement с первого дня — устраняет TOCTOU race при параллельных Task 4-9/сессиях:
-- SELECT-затем-INSERT (см. "Порядок и параллелизация") без БД-уровневой защиты допускает дубль
-- клеймов, если два скрипта одновременно проверят на существование и оба вставят. С этим
-- индексом вставка идёт через `INSERT ... ON CONFLICT (statement) DO NOTHING/UPDATE`, а не
-- через организационный порядок запуска.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_claims_statement ON agent_claims (statement);

-- === agent_runs — immutable-прогоны всех видов (dq | audit | experiment | proposal) ===
CREATE TABLE IF NOT EXISTS agent_runs (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('dq', 'audit', 'experiment', 'proposal')),
    name            TEXT NOT NULL,          -- машиночитаемое имя аудита/эксперимента, напр. "d42_portfolio_roi_replay"
    config_hash     TEXT NOT NULL,          -- hash параметров прогона (SQL/окно/фильтры) — воспроизводимость без LLM
    dataset_hash    TEXT NOT NULL,          -- hash снапшота исходных данных (row count + max(id)/max(updated_at) по источникам)
    query_version   TEXT,                   -- версия SQL/скрипта, если применимо (semver или git sha короткий)
    metrics         JSONB NOT NULL DEFAULT '{}',
    verdict         TEXT,                   -- свободный текст для Phase 0; фиксированный словарь (§4.2) — с Phase 1
    report_uri      TEXT,                   -- путь к markdown/JSON-отчёту прогона (research/ или docs/)
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_kind ON agent_runs (kind);
CREATE INDEX IF NOT EXISTS idx_agent_runs_name ON agent_runs (name);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at ON agent_runs (started_at);

-- === agent_actions — proposal + approval + receipt в одной сущности ===
-- Phase 0 эту таблицу не заполняет (Bounded Action Executor — Phase 3), но создаётся
-- сейчас вместе с остальными тремя, т.к. §5 фиксирует все 4 таблицы одной миграцией Phase 0.
CREATE TABLE IF NOT EXISTS agent_actions (
    id              BIGSERIAL PRIMARY KEY,
    proposal_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    kind            TEXT NOT NULL,          -- напр. "pause_detector", "reduce_cap", "close_position" (Phase 3 whitelist)
    level           TEXT NOT NULL CHECK (level IN ('L0', 'L1', 'L2', 'L3')),
    payload_hash    TEXT NOT NULL,          -- hash typed-полей proposal — approval подписывает ровно эти цифры
    payload         JSONB NOT NULL,
    idempotency_key TEXT NOT NULL,          -- session_id:call_id — защита от ретраев (см. gate_shadow_log дедуп-паттерн PR #703)
    status          TEXT NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed', 'approved', 'rejected', 'expired', 'executed', 'failed')),
    actor           TEXT,                   -- кто подтвердил (admin telegram id или 'agent' для L0/L1 auto)
    expires_at      TIMESTAMPTZ NOT NULL,
    executed_at     TIMESTAMPTZ,
    effect          JSONB,                  -- фактический эффект исполнения или причина отказа
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_actions_idempotency_key ON agent_actions (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_agent_actions_status ON agent_actions (status);
CREATE INDEX IF NOT EXISTS idx_agent_actions_proposal_id ON agent_actions (proposal_id);

-- === agent_llm_calls — учёт вызовов LLM ===
-- Phase 0 не делает LLM-вызовов синтеза (аудиты чисто детерминированные), но таблица
-- нужна с первого дня для cost ledger (§7.1) и на случай ручных LLM-вызовов при сид'е
-- knowledge base (Task 3 может опционально логировать сюда, если сид делался с помощью LLM).
CREATE TABLE IF NOT EXISTS agent_llm_calls (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT REFERENCES agent_runs (id),
    role            TEXT NOT NULL,          -- researcher | explainer | direction_scout_monitor | direction_scout_synthesis | judge | shadow_candidate
    provider        TEXT NOT NULL,          -- anthropic | openrouter | ...
    model           TEXT NOT NULL,
    prompt_version  TEXT,
    snapshot_hash   TEXT,                   -- hash входного снапшота (reconciliation)
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(10, 6) NOT NULL DEFAULT 0,
    schema_valid    BOOLEAN,                -- прошёл ли structured-output парсинг с первой попытки
    retry_count     INTEGER NOT NULL DEFAULT 0,
    reconciliation_ok BOOLEAN,              -- сошлись ли числа вывода со снапшотом (детерминированная сверка, не LLM)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_llm_calls_run_id ON agent_llm_calls (run_id);
CREATE INDEX IF NOT EXISTS idx_agent_llm_calls_created_at ON agent_llm_calls (created_at);
