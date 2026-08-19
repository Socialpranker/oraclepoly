-- 20260519125818_backfill_standalone_v2_yes_won.sql
-- Description: One-time backfill yes_won в standalone_v2_predictions из markets.winner.
--
-- Контекст 2026-05-19 (post-pipeline audit):
-- standalone_v2_predictions имеет 14,812 строк, но yes_won = NULL у ВСЕХ —
-- автоматический resolve-cron для этой таблицы никогда не настраивался.
-- 4,008 из них имеют связанный resolved market (markets.resolved=true и
-- winner IN ('yes','no')) — можем заполнить yes_won detrministically.
--
-- Используется для L2 retrain dataset (combined с pipeline_v2_shadow_predictions).
-- Не destructive: только UPDATE NULL → 0/1.
--
-- Идемпотентность: WHERE sv.yes_won IS NULL — повторный прогон ничего не делает.

UPDATE standalone_v2_predictions sv
   SET yes_won = CASE WHEN m.winner = 'yes' THEN 1 ELSE 0 END,
       resolved_at = COALESCE(sv.resolved_at, m.updated_at, now())
  FROM markets m
 WHERE m.polymarket_id = sv.market_id
   AND m.resolved = true
   AND m.winner IN ('yes', 'no')
   AND sv.yes_won IS NULL;

-- Sanity: после UPDATE должно быть 4008 (или меньше если за время с момента
-- audit'а появились новые) labeled rows в standalone_v2_predictions.
