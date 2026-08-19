-- 20260613140000_create_mv_market_calibration.sql
-- Description: Materialized view честной калибровки рынка (market price vs actual outcome)
-- на НЕотобранной детекторами выборке (daily_price_snapshots ⋈ markets.winner).
--
-- Цель: отличать реальный calibration-edge от selection-bias. Прошлый "YES overpriced"
-- в detector_signals был артефактом отбора детекторов (они ищут переоценённый YES).
-- На нейтральной выборке gap по бакетам ±0.05 (рынок откалиброван).
--
-- Источники (проверено на проде 2026-06-13):
--   - daily_price_snapshots: 876K снимков 2024-01-01..сегодня, PK (condition_id, snapshot_date)
--   - markets.winner: lowercase 'yes'/'no', 114621 резолвнутых, 0 без winner
--   - JOIN по condition_id; горизонт = markets.end_date::date - snapshot_date
--
-- Refresh hourly (REFRESH MATERIALIZED VIEW CONCURRENTLY — требует UNIQUE INDEX).

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_market_calibration AS
WITH snaps AS (
    SELECT
        dps.condition_id,
        dps.yes_price::numeric                                   AS yes_price,
        (m.end_date::date - dps.snapshot_date)                  AS days_before,
        m.category,
        CASE WHEN m.winner = 'yes' THEN 1 ELSE 0 END            AS yes_won,
        -- horizon bucket
        CASE
            WHEN (m.end_date::date - dps.snapshot_date) BETWEEN 1 AND 2   THEN 'near'
            WHEN (m.end_date::date - dps.snapshot_date) BETWEEN 3 AND 7   THEN 'mid'
            WHEN (m.end_date::date - dps.snapshot_date) BETWEEN 8 AND 30  THEN 'far'
            WHEN (m.end_date::date - dps.snapshot_date) BETWEEN 31 AND 365 THEN 'deep'
        END                                                     AS horizon_bucket,
        -- price bucket index 0..19 (шаг 0.05)
        LEAST(FLOOR(dps.yes_price::numeric * 20)::int, 19)      AS price_bucket
    FROM daily_price_snapshots dps
    JOIN markets m ON m.condition_id = dps.condition_id
    WHERE m.winner IN ('yes', 'no')
      AND dps.yes_price BETWEEN 0.02 AND 0.98
      AND (m.end_date::date - dps.snapshot_date) BETWEEN 1 AND 365
),
-- дедуп: 1 снимок на (condition_id, horizon_bucket) — ближайший к центру окна
dedup AS (
    SELECT DISTINCT ON (condition_id, horizon_bucket)
        condition_id, horizon_bucket, price_bucket, yes_price, yes_won, category, days_before
    FROM snaps
    -- defensive: при штатных границах (BETWEEN 1 AND 365 в snaps) horizon_bucket всегда NOT NULL;
    -- этот фильтр страхует от рассогласования WHERE-границ и CASE-веток выше
    WHERE horizon_bucket IS NOT NULL
    ORDER BY condition_id, horizon_bucket,
        ABS(days_before - CASE horizon_bucket
            WHEN 'near' THEN 1.5 WHEN 'mid' THEN 5 WHEN 'far' THEN 19 WHEN 'deep' THEN 90 END)
),
agg AS (
    SELECT
        horizon_bucket,
        price_bucket,
        COALESCE(category, 'Unknown')                          AS category,
        COUNT(*)                                               AS n,
        AVG(yes_price)                                         AS implied_prob,
        AVG(yes_won::numeric)                                  AS actual_winrate,
        AVG(POWER(yes_price - yes_won, 2))                     AS brier
    FROM dedup
    GROUP BY horizon_bucket, price_bucket, COALESCE(category, 'Unknown')
)
SELECT
    horizon_bucket,
    price_bucket,
    category,
    n,
    ROUND(implied_prob, 4)                                     AS implied_prob,
    ROUND(actual_winrate, 4)                                   AS actual_winrate,
    ROUND(actual_winrate - implied_prob, 4)                    AS calib_gap,
    ROUND(brier, 4)                                            AS brier,
    -- Wilson 95% CI lower/upper на actual_winrate (z=1.96)
    ROUND(
        ((actual_winrate + (1.96*1.96)/(2*n))
         - 1.96 * SQRT((actual_winrate*(1-actual_winrate) + (1.96*1.96)/(4*n)) / n))
        / (1 + (1.96*1.96)/n), 4)                              AS wilson_lower,
    ROUND(
        ((actual_winrate + (1.96*1.96)/(2*n))
         + 1.96 * SQRT((actual_winrate*(1-actual_winrate) + (1.96*1.96)/(4*n)) / n))
        / (1 + (1.96*1.96)/n), 4)                              AS wilson_upper
FROM agg;

-- UNIQUE INDEX обязателен для REFRESH CONCURRENTLY
CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_market_calibration_key
    ON mv_market_calibration (horizon_bucket, price_bucket, category);

CREATE INDEX IF NOT EXISTS idx_mv_market_calibration_gap
    ON mv_market_calibration (ABS(calib_gap) DESC)
    WHERE n >= 40;

COMMENT ON MATERIALIZED VIEW mv_market_calibration IS
    'Калибровка рынка (implied price vs actual outcome) на НЕотобранной выборке daily_price_snapshots. '
    'calib_gap = actual_winrate - implied_prob (отриц = YES переоценён). Wilson CI на N. '
    'Refresh hourly. Edge реален только при n>=40 И wilson_lower/upper не пересекает gap=0.';
