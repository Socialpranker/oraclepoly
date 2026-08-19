-- Migration: dry_strategy_pruning
-- Отключить слабые dry стратегии и ребалансировать bankroll
--
-- Критерий отключения: resolved < 10 И ROI на wagered < 30%
-- Защита: не отключать лучшую стратегию на каждый signal_source

-- 1. Отключить слабые стратегии
UPDATE strategy_state
SET stage = 'disabled'
WHERE stage = 'dry'
  AND strategy_key IN (
    SELECT ss.strategy_key
    FROM strategy_state ss
    LEFT JOIN (
        SELECT
            strategy_key,
            COUNT(*) FILTER (WHERE status IN ('won','lost')) AS resolved,
            COALESCE(SUM(pnl) FILTER (WHERE status IN ('won','lost')), 0) AS total_pnl,
            COALESCE(SUM(stake) FILTER (WHERE status IN ('won','lost')), 0) AS total_wagered
        FROM strategy_positions
        WHERE mode = 'dry'
        GROUP BY strategy_key
    ) sp ON sp.strategy_key = ss.strategy_key
    WHERE ss.stage = 'dry'
      -- слабая: мало данных (< 10) И плохой ROI (< 30% на wagered)
      AND (sp.resolved IS NULL OR sp.resolved < 10)
      AND (
          sp.total_wagered IS NULL
          OR sp.total_wagered = 0
          OR (sp.total_pnl / sp.total_wagered) < 0.30
      )
      -- защита: не отключать лучшую стратегию на каждый signal_source
      AND ss.strategy_key NOT IN (
          SELECT DISTINCT ON (ss2.signal_source) ss2.strategy_key
          FROM strategy_state ss2
          LEFT JOIN (
              SELECT
                  strategy_key,
                  COALESCE(SUM(pnl) FILTER (WHERE status IN ('won','lost')), 0) AS total_pnl,
                  COALESCE(SUM(stake) FILTER (WHERE status IN ('won','lost')), 0) AS total_wagered
              FROM strategy_positions WHERE mode = 'dry'
              GROUP BY strategy_key
          ) best ON best.strategy_key = ss2.strategy_key
          WHERE ss2.stage = 'dry'
          ORDER BY
              ss2.signal_source,
              CASE WHEN best.total_wagered > 0 THEN best.total_pnl / best.total_wagered ELSE -999 END DESC
      )
  );

-- 2. Синхронизировать stage в strategy_bankroll
UPDATE strategy_bankroll sb
SET stage = 'disabled'
FROM strategy_state ss
WHERE sb.strategy_key = ss.strategy_key
  AND ss.stage = 'disabled'
  AND sb.stage = 'dry';

-- 3. Ребалансировать initial_bankroll активных dry стратегий
--    Целевой портфель: $10,000 равномерно, max $500, min $200
UPDATE strategy_bankroll
SET
    initial_bankroll = LEAST(500.0, GREATEST(200.0, 10000.0 / (
        SELECT COUNT(*) FROM strategy_state WHERE stage = 'dry'
    )))
WHERE strategy_key IN (
    SELECT strategy_key FROM strategy_state WHERE stage = 'dry'
);
