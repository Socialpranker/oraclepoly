-- 20260502160410_dry_run_cleanup_and_virtual_stage.sql
-- Migration для plan: detector-lifecycle-ui-architecture
--
-- Изменения:
-- 1. UPDATE legacy 'dry_run' статус → 'virtual_placed' (Pipeline V1 cleanup)
-- 2. ALTER detector_state.stage CHECK constraint:
--    'shadow'|'live'|'paused' → 'shadow'|'virtual'|'live'|'paused'
--    Это включает двухступенчатый promote: Shadow → Virtual → Live.
--
-- Effect для prod: текущие 7 production детекторов остаются stage='shadow'.
-- Юзер вручную promote'ит каждый через TG callback в virtual после 7-14d
-- наблюдения, потом в live после virtual validation.

BEGIN;

-- 1. Cleanup legacy dry_run (22366 rows на проде на 2026-05-02)
UPDATE execution_orders
SET status = 'virtual_placed'
WHERE status = 'dry_run';

-- 2. Расширить detector_state.stage до 4 значений
ALTER TABLE detector_state
    DROP CONSTRAINT IF EXISTS detector_state_stage_check;

ALTER TABLE detector_state
    ADD CONSTRAINT detector_state_stage_check
    CHECK (stage IN ('shadow', 'virtual', 'live', 'paused'));

COMMIT;
