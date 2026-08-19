-- 20260720162346_create_claims_expiry_queue_view.sql
-- Description: claims_expiry_queue — read model (§5 спеки, docs/specs/portfolio_analyst_agent.md)
-- поверх agent_claims: список клеймов с вычисленной датой протухания. Питает
-- app/jobs/agent_daily_brief.py (Task 10, protухающие клеймы за 3 дня).
-- SQL — дословно из Task 10 брифа Step 1 (.superpowers/sdd/task-10-brief.md).
--
-- REFRESH: не автоматизирован отдельным cron-джобом в этой миграции. Решение
-- задокументировано в app/jobs/agent_daily_brief.py docstring — джоб сам
-- вызывает REFRESH MATERIALIZED VIEW перед чтением (MV маленькая, agent_claims
-- Phase 0 — десятки строк, не проблема производительности раз в день).

CREATE MATERIALIZED VIEW IF NOT EXISTS claims_expiry_queue AS
SELECT id, statement, scope, status, verify_policy, verify_halflife_days, last_verified_at,
       CASE
         WHEN verify_policy = 'time_halflife' AND verify_halflife_days IS NOT NULL
           THEN last_verified_at + (verify_halflife_days || ' days')::interval
         ELSE NULL
       END AS expires_at
FROM agent_claims
WHERE status IN ('active', 'testing');
CREATE UNIQUE INDEX IF NOT EXISTS uq_claims_expiry_queue_id ON claims_expiry_queue (id);
