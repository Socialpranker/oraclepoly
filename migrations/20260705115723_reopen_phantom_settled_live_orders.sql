-- 20260705115723_reopen_phantom_settled_live_orders.sql
-- Ремонт P0 settle-по-EXPIRED (research/fable-gates-audit-2026-07-03 §3.1):
-- 7 live-ордеров закрыты в $0 по таймауту СИГНАЛА при нерезолвнутом рынке —
-- реальные шэры живут на бирже. Возвращаем в 'filled'; после резолва рынка их
-- корректно закроет починенный _settle_filled (fix в том же PR).
-- НЕ трогаем dccc70ab (2026-04-29, outcome=NULL, stake $1) — другой артефакт,
-- ручное решение отдельно.
UPDATE execution_orders eo
   SET status          = 'filled',
       outcome         = NULL,
       pnl             = NULL,
       -- trading_fee_usd/slippage_usd — NOT NULL DEFAULT 0: сбрасываем в 0,
       -- реальные значения перезапишет _SETTLE_UPDATE_SQL при честном settle.
       trading_fee_usd = 0,
       slippage_usd    = 0,
       net_pnl         = NULL,
       settled_at      = NULL,
       exit_reason     = NULL
  FROM markets m
 WHERE m.polymarket_id = eo.market_id
   AND eo.status = 'settled'
   AND eo.execution_mode IN ('maker', 'taker')
   AND eo.outcome = 'EXPIRED'
   AND eo.exit_reason = 'resolution'
   AND m.resolved = false;
