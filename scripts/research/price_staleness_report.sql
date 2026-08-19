-- Гейт-анализ План 1 → План 2 (запускать через 3-7 дней после деплоя телеметрии).
-- docs/superpowers/plans/2026-07-03-event-driven-price-pipeline.md, секция «Гейт».
SELECT decision_point,
       COUNT(*)                                                        AS n,
       ROUND(AVG(is_live::int) * 100, 1)                               AS live_coverage_pct,
       ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY delta_abs)::numeric, 4) AS p50_delta,
       ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY delta_abs)::numeric, 4) AS p95_delta,
       ROUND(MAX(delta_abs)::numeric, 4)                               AS max_delta
  FROM price_staleness_log
 WHERE logged_at > now() - interval '7 days'
 GROUP BY decision_point
 ORDER BY decision_point;
