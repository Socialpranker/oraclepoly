-- 20260501120002_execution_orders_v2.sql
-- Description: Adapt execution_orders for V2 pipeline (detector_signal-based, not strategy-based).
-- Drops NOT NULL on legacy strategy_key, adds detector linkage and ML probability snapshot.
-- Two-layer idempotency:
--   1) UNIQUE(detector_signal_id) — one intent per signal (cron retries safe).
--   2) UNIQUE(detector, market_id, side) WHERE status IN open states — one open position per (detector, market, side).

ALTER TABLE public.execution_orders
    ALTER COLUMN strategy_key DROP NOT NULL;

ALTER TABLE public.execution_orders ADD COLUMN IF NOT EXISTS detector text;
ALTER TABLE public.execution_orders ADD COLUMN IF NOT EXISTS detector_signal_id uuid
    REFERENCES public.detector_signals(id) ON DELETE SET NULL;
ALTER TABLE public.execution_orders ADD COLUMN IF NOT EXISTS calibrated_p numeric(5,4);
ALTER TABLE public.execution_orders ADD COLUMN IF NOT EXISTS meta_p numeric(5,4);
ALTER TABLE public.execution_orders ADD COLUMN IF NOT EXISTS ev_pct numeric(7,2);
ALTER TABLE public.execution_orders ADD COLUMN IF NOT EXISTS bankroll_at_entry numeric(10,2);
ALTER TABLE public.execution_orders ADD COLUMN IF NOT EXISTS rejection_reason text;

-- Полный UNIQUE INDEX (без WHERE), чтобы ON CONFLICT (detector_signal_id) работал.
-- В Postgres standard NULL ≠ NULL для UNIQUE → legacy строки с NULL не конфликтуют.
CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_orders_signal
    ON public.execution_orders (detector_signal_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_orders_open_market
    ON public.execution_orders (detector, market_id, side)
    WHERE status IN ('intent', 'placed', 'filled');

CREATE INDEX IF NOT EXISTS idx_execution_orders_detector_status
    ON public.execution_orders (detector, status, created_at DESC)
    WHERE detector IS NOT NULL;
