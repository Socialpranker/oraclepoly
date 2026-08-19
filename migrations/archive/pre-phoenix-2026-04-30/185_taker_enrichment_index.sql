-- 185: Partial index для taker enrichment backfill query
-- WHERE is_taker IS NULL AND created_at > X — ускоряет hourly cron

CREATE INDEX IF NOT EXISTS idx_transactions_is_taker_null
    ON transactions(created_at DESC)
    WHERE is_taker IS NULL;
