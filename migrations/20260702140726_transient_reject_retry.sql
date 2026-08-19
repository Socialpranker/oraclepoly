-- 20260702140726_transient_reject_retry.sql
-- Description: transient-reject ретраится (audit топ-4): partial UNIQUE + маркер транзиентности.
-- rejected/cancelled строки больше не занимают слот сигнала навсегда.

ALTER TABLE public.execution_orders
    ADD COLUMN IF NOT EXISTS is_transient_reject boolean NOT NULL DEFAULT false;

DROP INDEX IF EXISTS uq_execution_orders_signal;

-- Один АКТИВНЫЙ ордер на сигнал; rejected/cancelled — аудит-след, не блокирует ретрай.
CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_orders_signal_active
    ON public.execution_orders (detector_signal_id)
    WHERE status NOT IN ('rejected', 'cancelled');
