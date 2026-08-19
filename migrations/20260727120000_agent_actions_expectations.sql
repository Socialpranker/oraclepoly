-- 20260727120000_agent_actions_expectations.sql
-- Description: Phase 2 C4 — колонки ожидания и сверки в agent_actions.
-- Спека: docs/superpowers/specs/2026-07-27-phase2-c4-decision-reconcile-design.md §3.
-- Все колонки nullable: строки не-ACK действий и существующие строки остаются
-- валидными без backfill (в проде agent_actions = 0 строк на 2026-07-27).
-- status остаётся 'executed' (семантика C3b «человек нажал») — открытость
-- ожидания выражается через reconciled_at IS NULL, CHECK не расширяем.

ALTER TABLE agent_actions
    ADD COLUMN IF NOT EXISTS due_date              TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reconciled_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reconciliation_result JSONB;

-- Partial index — выборка «пора сверять» джобой decision_reconcile.
-- Паттерн проекта: все «пора обработать» выборки идут по индексированным колонкам.
CREATE INDEX IF NOT EXISTS idx_agent_actions_due_pending
    ON agent_actions (due_date)
    WHERE due_date IS NOT NULL AND reconciled_at IS NULL;
