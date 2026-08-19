-- 20260501215910_ml_market_picker_add_mode.sql
-- Description: Поддержать одновременно binary и ev_regression модели в ml_market_picker.
--
-- Контекст: legacy L2 учится на outcome ∈ {0,1} (binary). Это не различает
-- "WIN с net_roi=+5%" и "WIN с net_roi=+1000%" — обе записи как выигрышные.
-- В результате модель открывает позиции типа NO @ 0.99 (потенциал +1%, fee
-- съедает) с тем же confidence что и на NO @ 0.05 (потенциал +1900%).
--
-- Решение: A/B запуск двух моделей параллельно:
-- - mode='binary' (legacy, активная сейчас) — открывает позиции
-- - mode='ev_regression' (новая, predicts realized_roi_net напрямую) — пишет
--   shadow predictions, не открывает позиции. Через неделю сравним по net PnL.
--
-- uniqueness gate (uq_ml_market_picker_active) меняем на partial по mode:
-- одна active модель на каждый mode. Это позволяет иметь и binary live и
-- ev_regression shadow одновременно.

ALTER TABLE public.ml_market_picker
    ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'binary'
        CHECK (mode IN ('binary', 'ev_regression'));

-- Старый partial index был UNIQUE WHERE is_active = true.
-- Меняем на UNIQUE по (mode) WHERE is_active = true — одна active per mode.
DROP INDEX IF EXISTS uq_ml_market_picker_active;
CREATE UNIQUE INDEX uq_ml_market_picker_active_per_mode
    ON public.ml_market_picker (mode)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_ml_market_picker_mode
    ON public.ml_market_picker (mode, fit_at DESC);
