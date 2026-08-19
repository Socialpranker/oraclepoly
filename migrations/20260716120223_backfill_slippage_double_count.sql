-- 20260716120223_backfill_slippage_double_count.sql
-- Description: Backfill убрать двойной учёт slippage из net_pnl (fix 2026-07-16, аудит воронки D3).
-- fill_price уже отражал slippage → net_pnl был занижен на slippage_usd.
-- Скоуп: settled (live) + virtual_settled (promote-гейты D38/D42/D36 читают net_pnl).
UPDATE execution_orders
SET net_pnl = net_pnl + slippage_usd, slippage_usd = 0
WHERE status IN ('settled', 'virtual_settled')
  AND slippage_usd IS NOT NULL AND slippage_usd > 0;
