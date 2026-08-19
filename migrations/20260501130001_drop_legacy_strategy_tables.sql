-- 20260501130001_drop_legacy_strategy_tables.sql
-- Description: Удалить legacy strategy_* таблицы B2C-эпохи и pipeline_v2_shadow_positions
-- (заменён на execution_orders в V2). Backup создан перед drop:
-- /root/backups/legacy_strategy_phoenix_v3_20260501_123407.sql.gz

DROP TABLE IF EXISTS public.strategy_validations CASCADE;
DROP TABLE IF EXISTS public.strategy_audit CASCADE;
DROP TABLE IF EXISTS public.strategy_ledger CASCADE;
DROP TABLE IF EXISTS public.strategy_virtual_positions CASCADE;
DROP TABLE IF EXISTS public.strategy_positions CASCADE;
DROP TABLE IF EXISTS public.strategy_bankroll CASCADE;
DROP TABLE IF EXISTS public.strategy_state CASCADE;
DROP TABLE IF EXISTS public.strategy_discovery_runs CASCADE;
DROP TABLE IF EXISTS public.strategy_live_metrics CASCADE;
DROP TABLE IF EXISTS public.pipeline_v2_shadow_positions CASCADE;

-- Также legacy idempotency / circuit-breaker таблицы (если есть)
DROP TABLE IF EXISTS public.in_flight_locks CASCADE;
DROP TABLE IF EXISTS public.long_locks CASCADE;
