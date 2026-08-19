-- 20260416000004_purge_legacy_strategy_data.sql
-- Зачистка легаси-мусора, загрязняющего карты памяти и аналитику:
--   1. trade_signals от disabled/фантомных стратегий (85% данных — не используется).
--   2. strategy_audit записи тех же стратегий.
--   3. strategy_bankroll записи disabled-стратегий без активных позиций.
--
-- Критерий: стратегия не присутствует в strategy_state.stage IN ('dry','live').
-- Portfolio bankroll (_portfolio) и активные стратегии НЕ трогаем.

BEGIN;

-- 1. trade_signals: удалить всё от неактивных стратегий.
--    Используется только для live signal pipeline и strategy-positions job,
--    который читает только stage IN ('dry','live'). Старые записи — просто журнал.
DELETE FROM trade_signals
WHERE strategy_key NOT IN (
    SELECT strategy_key FROM strategy_state WHERE stage IN ('dry', 'live')
);

-- 2. strategy_audit: удалить записи фантомных/disabled стратегий.
DELETE FROM strategy_audit
WHERE strategy_key NOT IN (
    SELECT strategy_key FROM strategy_state WHERE stage IN ('dry', 'live')
);

-- 3. strategy_bankroll: удалить записи disabled-стратегий.
--    Оставляем _portfolio (special key) и всё, что в dry/live.
DELETE FROM strategy_bankroll
WHERE strategy_key != '_portfolio'
  AND strategy_key NOT IN (
      SELECT strategy_key FROM strategy_state WHERE stage IN ('dry', 'live')
  );

-- 4. Диагностика
DO $$
DECLARE
    ts_rows INT;
    sa_rows INT;
    sb_rows INT;
    ts_ghost INT;
    sb_ghost INT;
BEGIN
    SELECT COUNT(*) INTO ts_rows FROM trade_signals;
    SELECT COUNT(*) INTO sa_rows FROM strategy_audit;
    SELECT COUNT(*) INTO sb_rows FROM strategy_bankroll;
    SELECT COUNT(*) INTO ts_ghost FROM trade_signals ts
      WHERE ts.strategy_key NOT IN (SELECT strategy_key FROM strategy_state WHERE stage IN ('dry','live'));
    SELECT COUNT(*) INTO sb_ghost FROM strategy_bankroll sb
      WHERE sb.strategy_key != '_portfolio'
        AND sb.strategy_key NOT IN (SELECT strategy_key FROM strategy_state WHERE stage IN ('dry','live'));
    RAISE NOTICE 'trade_signals: % rows (ghost: %)', ts_rows, ts_ghost;
    RAISE NOTICE 'strategy_audit: % rows', sa_rows;
    RAISE NOTICE 'strategy_bankroll: % rows (ghost: %)', sb_rows, sb_ghost;
END $$;

COMMIT;
