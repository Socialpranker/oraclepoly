-- Drop legacy таблицы feature_store + oracle_recalc_timings (Phoenix V2 cleanup follow-up).
--
-- После Phoenix V2 (30 апреля 2026) writers этих таблиц удалены вместе с B2C
-- handlers (feature_store_refresh job, oracle_calculator instrumentation):
--
-- - feature_store (3.9 GB, 4.28M rows): pydantic модели feature_vector.py
--   и pipeline scoring оставлены (используют FeatureVector pydantic class
--   внутри runtime). Но FeatureStoreRepository никем не импортируется,
--   last_write 2026-04-30 04:00 — после deploy Phoenix больше не пишет.
--
-- - oracle_recalc_timings (519 MB, 3.87M rows): timing_instrumentation никем
--   не импортируется, last_write 2026-04-30 18:32 — отключён вместе с
--   oracle_calculator legacy.
--
-- Foreign keys: 0 (проверено через pg_constraint).
-- Active callers: 0 (grep по всем app/ + scripts/).
--
-- Effect: -4.4 GB на 4 GB VPS.

DROP TABLE IF EXISTS feature_store CASCADE;
DROP TABLE IF EXISTS oracle_recalc_timings CASCADE;
