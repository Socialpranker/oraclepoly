-- Drop dead indexes — Phoenix V2 cleanup follow-up.
--
-- После Phoenix V2 (30 апреля 2026) удалили большинство B2C handlers
-- (smart_money, AI agents, weather_arbitrage, lab) но индексы которые
-- их обслуживали остались. Алерт DB Size 2026-05-01:
-- - oracle_snapshots: 1.7 GB (599 MB data + 1.12 GB indexes!)
-- - whales: 279 MB
--
-- Эта миграция убирает индексы с idx_scan ≤ 1000 за всё время жизни
-- (pg_stat_user_indexes), которые точно не используются после Phoenix:
--
-- oracle_snapshots:
--   idx_oracle_snapshots_vwap (163 MB, 413 scans) — legacy B2C VWAP analytics
--   idx_oracle_snapshots_calibration (63 MB, 228 scans) — старый calibrator
--   idx_oracle_snapshots_resolved_at_analytics (44 MB, 785 scans) — B2C analytics
--   idx_oracle_snapshots_category (36 MB, 3 scans) — purged
--   idx_oracle_snapshots_maker_direction (11 MB, 1 scan) — legacy
--
-- whales:
--   idx_whales_signal_strength (15 MB, 0 scans) — Phase D smart_money удалён
--   idx_whales_ebq_score (15 MB, 0 scans) — старая B2C метрика
--   idx_whales_is_arbitrageur (16 kB, 0 scans) — Phase F cleanup
--   idx_whales_cluster (8 kB, 0 scans) — Phase F cleanup
--   idx_whales_insider_score (8 kB, 0 scans) — Phase F cleanup
--
-- НЕ дропаем:
--   idx_oracle_snapshots_pending_recent (84 MB, 333 scans) — реально используется
--     в oracle_snapshot_repository.get_live_signals_data() через Index Only Scan,
--     EXPLAIN ANALYZE подтвердил активное использование (261ms на 112K rows).
--
-- Total освобождается: ~347 MB
--
-- После DROP — VACUUM FULL пройдёт отдельной командой (требует exclusive lock).

DROP INDEX IF EXISTS idx_oracle_snapshots_vwap;
DROP INDEX IF EXISTS idx_oracle_snapshots_calibration;
DROP INDEX IF EXISTS idx_oracle_snapshots_resolved_at_analytics;
DROP INDEX IF EXISTS idx_oracle_snapshots_category;
DROP INDEX IF EXISTS idx_oracle_snapshots_maker_direction;

DROP INDEX IF EXISTS idx_whales_signal_strength;
DROP INDEX IF EXISTS idx_whales_ebq_score;
DROP INDEX IF EXISTS idx_whales_is_arbitrageur;
DROP INDEX IF EXISTS idx_whales_cluster;
DROP INDEX IF EXISTS idx_whales_insider_score;
