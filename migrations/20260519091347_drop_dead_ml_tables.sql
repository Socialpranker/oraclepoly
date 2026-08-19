-- 20260519091347_drop_dead_ml_tables.sql
-- Description: Drop unused ML tables identified in ML stack audit.
--
-- Audit 2026-05-19:
--   ml_models                              — 0 rows, never populated. Legacy
--                                            placeholder для L2 LightGBM, который
--                                            на самом деле живёт в ml_meta_labeler.
--                                            Docstring в d42_event_driven_ml.py
--                                            упоминал его, но реализация не пишет.
--   _deprecated_ml_predictions_2026_05_05  — 0 rows, deprecated 2026-05-05, FK на
--                                            ml_models. Удаляется первым чтобы
--                                            развязать FK.
--   trade_features                         — 0 rows, never populated, feature store
--                                            placeholder из phoenix schema 2026-04-30,
--                                            никогда не использовался.
--
-- Все три таблицы пустые. Drop CASCADE на случай скрытых dependent objects.

DROP TABLE IF EXISTS public._deprecated_ml_predictions_2026_05_05 CASCADE;
DROP TABLE IF EXISTS public.ml_models CASCADE;
DROP TABLE IF EXISTS public.trade_features CASCADE;
