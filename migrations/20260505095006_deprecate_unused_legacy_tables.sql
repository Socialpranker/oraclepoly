-- 20260505095006_deprecate_unused_legacy_tables.sql
-- Description: Phase 1.3 cleanup — мягкий deprecation legacy таблиц.
--
-- Стратегия: RENAME (не DROP) — даём 7-day window на случай если что-то
-- упустили. Если через неделю ничего не упало — следующая миграция сделает
-- финальный DROP.
--
-- Затронутые таблицы:
--
-- 1. ml_predictions (223 MB) — Phoenix legacy (B2C era), 0 references в active code.
--    Verified: grep -rn 'ml_predictions' app/ scripts/ → только schema dump.
--    Заменено: standalone_v2_predictions + ml_meta_labeler.
--
-- 2. retail_trades (254 MB) — ingestion broken since 2026-04-30 (см. d13_scan.py:18).
--    Активные code refs все в комментариях "не используем больше".
--    Заменено: live_trade_buckets + Polymarket data-api inline fetch.
--
-- ВАЖНО: НЕ применяй этот миграцию автоматически — данные большие.
-- Ручной запуск после code review:
--   ssh prod && cat migration | docker exec -i oraclepoly-postgres psql ...

-- Step 1: Rename to _deprecated_* (data preserved, no production code reads from these)
ALTER TABLE IF EXISTS public.ml_predictions
    RENAME TO _deprecated_ml_predictions_2026_05_05;

ALTER TABLE IF EXISTS public.retail_trades
    RENAME TO _deprecated_retail_trades_2026_05_05;

-- Step 2: Add comment explaining timeline
COMMENT ON TABLE public._deprecated_ml_predictions_2026_05_05 IS
    'DEPRECATED 2026-05-05: Phoenix B2C legacy. Replaced by standalone_v2_predictions + ml_meta_labeler. DROP planned 2026-05-12 (after 7d safety window).';

COMMENT ON TABLE public._deprecated_retail_trades_2026_05_05 IS
    'DEPRECATED 2026-05-05: ingestion broken since 2026-04-30. Replaced by live_trade_buckets + Polymarket data-api inline fetch in detectors. DROP planned 2026-05-12.';
