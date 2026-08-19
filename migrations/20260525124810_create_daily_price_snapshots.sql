-- 20260525124810_create_daily_price_snapshots.sql
-- Description: Daily snapshot of every active market's yes_price for D47
-- mean-reversion и любых других rolling-window detectors.
--
-- Источник: markets.current_price (обновляется market-sync каждые 15 мин).
-- Cron: app/jobs/daily_price_snapshot.py каждый день в 00:05 UTC.
--
-- Storage cost: ~30K active markets × 1 row/day × ~50 bytes ≈ 1.5MB/day, ~550MB/year.
-- PK (condition_id, snapshot_date) — идемпотентность для повторного запуска.

CREATE TABLE IF NOT EXISTS public.daily_price_snapshots (
    condition_id    text          NOT NULL,
    snapshot_date   date          NOT NULL,
    yes_price       numeric(6,4)  NOT NULL,
    volume_24h      numeric(20,6),
    captured_at     timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (condition_id, snapshot_date)
);

-- Index для rolling-window lookups: WHERE condition_id = $1 AND snapshot_date BETWEEN now()-20d AND now()
-- PK уже покрывает это (leading column condition_id). Reverse index для bulk queries
-- по дате (backfill validation, cross-market analytics).
CREATE INDEX IF NOT EXISTS idx_daily_price_snapshots_date
    ON public.daily_price_snapshots (snapshot_date DESC);

COMMENT ON TABLE public.daily_price_snapshots IS
    'Daily snapshot of markets.yes_price for rolling-window detectors. See app/jobs/daily_price_snapshot.py';
