-- 20260320104300_drop_experiment_tables.sql
-- Phase 1: Drop experiment system tables (dashboard v2 refactoring)

DROP TABLE IF EXISTS experiment_snapshots CASCADE;
DROP TABLE IF EXISTS experiment_models CASCADE;
DROP TABLE IF EXISTS experiments CASCADE;
