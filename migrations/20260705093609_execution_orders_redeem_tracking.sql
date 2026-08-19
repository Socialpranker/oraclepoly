-- 20260705093609_execution_orders_redeem_tracking.sql
-- Description: трекинг on-chain redeem (claim выигрыша) для live-позиций.
--
-- v2_execution_resolve._settle_filled() пишет pnl/net_pnl как ЕСЛИ БЫ winning
-- shares выкупились по $1 (см. комментарий в _compute_settlement_pnl), но
-- реального вызова redeemPositions нигде не было — CTF-токены оставались на
-- funder-кошельке, USDC физически не поступал. redeem_job.py (app/jobs/) закрывает
-- этот разрыв через poly-web3 SDK (ProxyWeb3Service.redeem_all(), Polymarket Relayer).
--
-- redeem_status трекает исход ПОСЛЕДНЕЙ попытки редима для строки:
--   NULL      — ещё не редимили (или не WIN/live — большинство строк).
--   'redeemed' — Relayer подтвердил транзакцию (RedeemResult.success_list).
--   'failed'   — Relayer вернул ошибку для этого condition_id (RedeemResult.error_list);
--                следующий cron-тик попробует снова (redeem_status != 'redeemed').

ALTER TABLE public.execution_orders
    ADD COLUMN IF NOT EXISTS redeem_status text,
    ADD COLUMN IF NOT EXISTS redeemed_at timestamptz,
    ADD COLUMN IF NOT EXISTS redeem_error text;

COMMENT ON COLUMN execution_orders.redeem_status IS
    'NULL до первой попытки, redeemed после подтверждённого on-chain redeem, failed при ошибке Relayer (ретраится).';
COMMENT ON COLUMN execution_orders.redeemed_at IS
    'Момент подтверждения redeem-транзакции Polymarket Relayer. NULL пока не redeemed.';
COMMENT ON COLUMN execution_orders.redeem_error IS
    'Текст последней ошибки Relayer для этого condition_id, если redeem_status=failed.';

ALTER TABLE execution_orders
    ADD CONSTRAINT execution_orders_redeem_status_check
    CHECK (redeem_status IS NULL OR redeem_status IN ('redeemed', 'failed'));

-- Частичный индекс под основной SELECT redeem_job._fetch_redeem_candidates():
-- live + settled + WIN + ещё не redeemed.
CREATE INDEX IF NOT EXISTS idx_execution_orders_redeem_pending
    ON execution_orders (execution_mode, status, outcome)
    WHERE execution_mode = 'live' AND status = 'settled' AND outcome = 'WIN'
      AND redeem_status IS DISTINCT FROM 'redeemed';

-- Rollback (manual, не часть forward):
-- DROP INDEX IF EXISTS idx_execution_orders_redeem_pending;
-- ALTER TABLE execution_orders DROP CONSTRAINT IF EXISTS execution_orders_redeem_status_check;
-- ALTER TABLE execution_orders DROP COLUMN IF EXISTS redeem_status;
-- ALTER TABLE execution_orders DROP COLUMN IF EXISTS redeemed_at;
-- ALTER TABLE execution_orders DROP COLUMN IF EXISTS redeem_error;
