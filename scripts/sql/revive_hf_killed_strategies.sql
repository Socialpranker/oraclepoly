-- Revives the 7+ strategies killed on 2026-04-20 02:00 UTC by the broken validate_dry_strategies.py.
--
-- Root cause: the old script (research/validate_dry_strategies.py) ignored strategy_state.filters
-- (the real per-strategy JSONB filter dict) and instead applied hardcoded-by-name defaults.
-- Unknown strategies were collapsed to "NO on everything" which is unprofitable on 6-year HF data,
-- causing false-positive kills. The new Lab-based validation (this PR) uses real filters per strategy.
--
-- HOW TO APPLY (production write — must be approved by the owner):
--   ssh root@<YOUR_SERVER_IP>
--   docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly \
--       < ~/oracle/scripts/sql/revive_hf_killed_strategies.sql
--
-- Verify before and after:
--   SELECT strategy_key, stage, disabled_reason FROM strategy_state
--   WHERE disabled_reason LIKE 'hf_validation_failed%';

BEGIN;

UPDATE strategy_state
SET
    stage           = 'dry',
    disabled_reason = NULL,
    updated_at      = now()
WHERE
    stage           = 'disabled'
    AND disabled_reason = 'hf_validation_failed: full_hf PF<1.0 on 500K+ trades, killed 2026-04-20';

-- Verify: should report the number of revived strategies
SELECT
    strategy_key,
    stage,
    updated_at
FROM strategy_state
WHERE
    updated_at >= now() - INTERVAL '5 seconds'
    AND stage = 'dry'
ORDER BY strategy_key;

COMMIT;
