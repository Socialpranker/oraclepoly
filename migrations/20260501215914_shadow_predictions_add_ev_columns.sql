-- 20260501215910_shadow_predictions_add_ev_columns.sql
-- Description: Хранить EV-regressor predictions рядом с binary monolith/v2 для A/B.
--
-- Контекст: новый L2 (mode='ev_regression') предсказывает expected net_roi
-- напрямую (continuous, [-1, +5]) вместо binary P(WIN). На неделю запускаем
-- его в shadow mode — пишет predictions в эту же таблицу новой колонкой.
-- Через 7 дней сравним с binary v2_meta_p по реализованному net_pnl.
--
-- v2_ev_pred: предсказанный net_roi от EV-regressor (NULL пока модель не fit'нута
-- или ev_regressor inference path не активен).
-- v2_ev_version: который EV-regressor дал предсказание (audit trail).

ALTER TABLE public.pipeline_v2_shadow_predictions
    ADD COLUMN IF NOT EXISTS v2_ev_pred    numeric(8,5),
    ADD COLUMN IF NOT EXISTS v2_ev_version text;

CREATE INDEX IF NOT EXISTS idx_p2_shadow_pred_ev_resolved
    ON public.pipeline_v2_shadow_predictions (resolved, v2_ev_pred)
    WHERE resolved = true AND v2_ev_pred IS NOT NULL;
