-- 20260723135607_unique_policy_proposal_hash.sql
-- Description: Phase 2 C1 — DB-страховка дедупа proposals policy engine
-- (TOCTOU: cooldown-SELECT в _persist_proposals затем отдельный INSERT — гонка возможна
-- при ручном перезапуске джобы поверх незавершённого cron-прогона; agent_runs.config_hash
-- сам по себе не уникален).
--
-- Partial: только policy-engine proposals; чужие kind='proposal' записи (Phase 0 free-form)
-- не трогаем.

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_policy_proposal_hash
    ON agent_runs (config_hash)
    WHERE kind = 'proposal' AND query_version = 'policy_engine_daily_v1';
