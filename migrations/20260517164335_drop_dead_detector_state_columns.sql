-- 20260517164335_drop_dead_detector_state_columns.sql
-- Description: Drop dead columns from detector_state — n_resolved, wr_30d,
-- pnl_total_usd, drawdown_pct никогда не обновлялись (нет UPDATE в коде),
-- никогда не использовались для решений (execution_safety читает только stage).
-- Live-метрики per detector агрегируются на лету из execution_orders/signals
-- в app/api/bot.py — это и есть SSoT. Удаляем мёртвые поля чтобы убрать
-- confusing state (все значения = 0 после миграции 2026-05-11 → ложное
-- "детектор не торговал").

ALTER TABLE public.detector_state
    DROP COLUMN IF EXISTS n_resolved,
    DROP COLUMN IF EXISTS wr_30d,
    DROP COLUMN IF EXISTS pnl_total_usd,
    DROP COLUMN IF EXISTS drawdown_pct;
