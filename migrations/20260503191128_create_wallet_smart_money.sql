-- 20260503191128_create_wallet_smart_money.sql
-- Wallet smart money classification for D13 RetailConfidenceFade detector.
-- Source: /mnt/data/canonical/wallet_performance.parquet (PR #417)
--
-- Populates from canonical wallet_performance: 2.28M total wallets, 17,330
-- classified as smart money (≥50 markets, ≥$10K vol, ROI ≥5%). Detector D13
-- uses this to compute smart_money_taker_share per market при emission.

CREATE TABLE IF NOT EXISTS public.wallet_smart_money (
    wallet TEXT PRIMARY KEY,
    n_resolved_markets BIGINT NOT NULL,
    usd_volume_taker DOUBLE PRECISION NOT NULL,
    realized_pnl_usd DOUBLE PRECISION NOT NULL,
    roi_pct DOUBLE PRECISION NOT NULL,
    win_rate_market_level DOUBLE PRECISION NOT NULL,
    bigshot_class TEXT NOT NULL,
    is_smart_money BOOLEAN NOT NULL DEFAULT FALSE,
    is_top_decile BOOLEAN NOT NULL DEFAULT FALSE,
    last_trade_ts BIGINT NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wallet_smart_money_smart
    ON public.wallet_smart_money (wallet) WHERE is_smart_money = TRUE;

CREATE INDEX IF NOT EXISTS idx_wallet_smart_money_top_decile
    ON public.wallet_smart_money (wallet) WHERE is_top_decile = TRUE;

COMMENT ON TABLE public.wallet_smart_money IS
    'Wallet quality classifications for D13 detector. Refresh from canonical wallet_performance.parquet via scripts/jobs/refresh_wallet_smart_money.py weekly.';
