-- 20260707093221_category_performance_views.sql
-- Description: event-кластеризованная аналитика P&L по категориям (live vs virtual).
--
-- Мотивация (аудит 2026-07-07): per-order агрегаты по категориям врут — бот входит
-- в один рынок десятками коррелированных ордеров (BTC-рынки: 32-38 входов), а стороны
-- одного события (кандидаты одних выборов) — это одна ставка, посчитанная многократно.
-- Единица наблюдения для ранжирования категорий = событие (markets.event_id), не ордер.
--
-- event_performance    — один ряд на событие × режим × детектор (кластер).
-- category_performance — агрегат 60д поверх кластеров + метрики концентрации
--                        (best/worst событие, медианный ROI события).
--
-- EXPIRED исключён из staked/net_pnl (PnL=0 by design, разбавляет ROI),
-- VOIDED_BUG исключён целиком (занулённые баги, не наблюдения).

CREATE OR REPLACE VIEW event_performance AS
SELECT
    m.event_id,
    COALESCE(m.category, 'other')                                          AS category,
    CASE WHEN eo.execution_mode = 'virtual' THEN 'virtual' ELSE 'live' END AS mode,
    COALESCE(ds.detector, 'unknown')                                       AS detector,
    COUNT(*)                                                               AS orders,
    COUNT(*) FILTER (WHERE eo.outcome = 'WIN')                             AS wins,
    COUNT(*) FILTER (WHERE eo.outcome = 'LOSS')                            AS losses,
    COUNT(*) FILTER (WHERE eo.outcome = 'EARLY_EXIT')                      AS early_exits,
    COUNT(*) FILTER (WHERE eo.outcome = 'EXPIRED')                         AS expired,
    COALESCE(SUM(eo.stake) FILTER (WHERE eo.outcome <> 'EXPIRED'), 0)      AS staked,
    COALESCE(SUM(eo.net_pnl) FILTER (WHERE eo.outcome <> 'EXPIRED'), 0)    AS net_pnl,
    SUM(eo.net_pnl) FILTER (WHERE eo.outcome <> 'EXPIRED')
        / NULLIF(SUM(eo.stake) FILTER (WHERE eo.outcome <> 'EXPIRED'), 0)  AS roi,
    MIN(eo.settled_at)                                                     AS first_settled_at,
    MAX(eo.settled_at)                                                     AS last_settled_at
FROM execution_orders eo
JOIN markets m ON m.polymarket_id = eo.market_id
LEFT JOIN detector_signals ds ON ds.id = eo.detector_signal_id
WHERE eo.status IN ('settled', 'virtual_settled')
  AND eo.outcome IN ('WIN', 'LOSS', 'EARLY_EXIT', 'EXPIRED')
GROUP BY m.event_id, 2, 3, 4;

CREATE OR REPLACE VIEW category_performance AS
SELECT
    category,
    mode,
    detector,
    COUNT(*)                                              AS events,
    SUM(orders)                                           AS orders,
    SUM(wins)                                             AS wins,
    SUM(losses)                                           AS losses,
    SUM(early_exits)                                      AS early_exits,
    SUM(expired)                                          AS expired,
    SUM(staked)                                           AS staked,
    SUM(net_pnl)                                          AS net_pnl,
    SUM(net_pnl) / NULLIF(SUM(staked), 0)                 AS roi,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY roi)      AS median_event_roi,
    MAX(net_pnl)                                          AS best_event_pnl,
    MIN(net_pnl)                                          AS worst_event_pnl
FROM event_performance
WHERE last_settled_at > now() - interval '60 days'
GROUP BY category, mode, detector;
