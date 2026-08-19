-- migrations/20260413000002_cancel_stale_pending_orders.sql
-- Зачистить pending ордера у которых placed_at IS NULL и рынок уже закрыт/протух.
-- Это 5 ордеров с 2026-04-08 которые зависли — ставки так и не дошли до Polymarket.

UPDATE execution_orders
SET
    status        = 'cancelled',
    cancel_reason = 'stale_pending_market_closed',
    cancelled_at  = NOW()
WHERE
    status     = 'pending'
    AND placed_at IS NULL
    AND created_at < NOW() - INTERVAL '12 hours';
