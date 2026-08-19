-- migrations/171_transactions_is_taker.sql
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS is_taker BOOLEAN DEFAULT NULL;
COMMENT ON COLUMN transactions.is_taker IS 'true=taker (aggressive), false=maker (passive), null=unknown';
