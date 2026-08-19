-- 20260505084324_create_live_feature_tables.sql
-- Description: Live ML feature pipeline storage layer (Variant A — universal).
--
-- 4 таблицы для incremental computation 55-col ml_features над active markets:
--   1. live_price_snapshots       — yes_price на каждом anchor_h в момент когда anchor_ts = now
--   2. live_trade_buckets         — hourly aggregates trades (mirror canonical schema)
--   3. live_wallet_flow_per_market — per (market, hour) wallet metrics (smart_money JOIN)
--   4. live_ml_features           — финальная 55-col matrix, читаемая ML detectors
--
-- Retention: rolling 30 дней (для самой длинной feature window 720h = 30d).
-- Cleanup делается в writer'ах (DELETE WHERE hour_bucket < now() - INTERVAL '31 days').
-- См. docs/superpowers/plans/bright-hopping-meteor.md (Day 2).

-- ─── 1. Price snapshots: 1 row per (market × anchor_hours) ───
CREATE TABLE IF NOT EXISTS live_price_snapshots (
    condition_id      text        NOT NULL,
    hours_to_resolve  int         NOT NULL,
    yes_price         numeric(8, 6) NOT NULL,
    captured_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (condition_id, hours_to_resolve)
);

COMMENT ON TABLE live_price_snapshots IS
    'Mirror canonical price_snapshots for active markets. Writer captures yes_price at moment when anchor_ts (=end_date - hours_to_resolve) passes through now().';

-- ─── 2. Trade buckets: hourly aggregates from Polymarket data-api trades ───
CREATE TABLE IF NOT EXISTS live_trade_buckets (
    condition_id      text        NOT NULL,
    hour_bucket       timestamptz NOT NULL,
    n_trades          int         NOT NULL,
    total_volume_usd  numeric     NOT NULL,
    vwap_yes          numeric(8, 6),
    min_yes_price     numeric(8, 6),
    max_yes_price     numeric(8, 6),
    buy_volume        numeric     NOT NULL DEFAULT 0,
    sell_volume       numeric     NOT NULL DEFAULT 0,
    captured_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (condition_id, hour_bucket)
);

CREATE INDEX IF NOT EXISTS idx_live_trade_buckets_bucket
    ON live_trade_buckets (hour_bucket);

COMMENT ON TABLE live_trade_buckets IS
    'Mirror canonical trade_buckets schema. Writer fetches trades from data-api.polymarket.com per active market every 15 min and aggregates into hourly buckets. Retention 30d via DELETE in writer.';

-- ─── 3. Wallet flow: derived from live_trade_buckets JOIN wallet_smart_money ───
CREATE TABLE IF NOT EXISTS live_wallet_flow_per_market (
    condition_id            text        NOT NULL,
    hour_bucket             timestamptz NOT NULL,
    n_unique_takers         int         NOT NULL,
    n_unique_makers         int         NOT NULL,
    whale_taker_volume      numeric     NOT NULL DEFAULT 0,
    whale_taker_count       int         NOT NULL DEFAULT 0,
    retail_taker_avg_size   numeric,
    whale_buy_pressure      numeric     NOT NULL DEFAULT 0,
    taker_count_total       int         NOT NULL DEFAULT 0,
    captured_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (condition_id, hour_bucket)
);

CREATE INDEX IF NOT EXISTS idx_live_wallet_flow_bucket
    ON live_wallet_flow_per_market (hour_bucket);

COMMENT ON TABLE live_wallet_flow_per_market IS
    'Mirror canonical wallet_flow_per_market schema. Writer joins live_trade_buckets against wallet_smart_money to compute whale_taker_volume, n_unique_takers, etc. Smart money set refreshed weekly.';

-- ─── 4. Final feature matrix: 55 cols + metadata, ML detectors read this ───
CREATE TABLE IF NOT EXISTS live_ml_features (
    market_id      text        NOT NULL,
    anchor_hours   int         NOT NULL,
    computed_at    timestamptz NOT NULL DEFAULT now(),

    -- ─── Static metadata ───
    event_id           text,
    yes_won            boolean,            -- NULL для active markets (нет outcome)
    category           text,
    neg_risk           int,
    horizon_days       numeric,
    resolve_hour_utc   int,
    resolve_dow        int,
    resolve_month      int,
    resolve_year       int,
    n_markets_in_event int,

    -- ─── Anchor + earlier prices ───
    yes_price_at_anchor   numeric(8, 6),
    yes_price_24h         numeric(8, 6),
    yes_price_72h         numeric(8, 6),
    yes_price_168h        numeric(8, 6),
    yes_price_720h        numeric(8, 6),

    -- ─── Drift ───
    drift_72h_to_anchor   numeric,
    drift_7d_to_anchor    numeric,

    -- ─── As-of cumulative aggregates ───
    hours_with_trades_asof  int,
    n_trades_asof           int,
    volume_asof             numeric,
    vwap_asof               numeric,
    price_min_asof          numeric,
    price_max_asof          numeric,
    price_range_asof        numeric,
    buy_ratio_asof          numeric,
    buy_imbalance_asof      numeric,

    -- ─── Recent flow ───
    vol_24h_before_anchor       numeric,
    trades_24h_before_anchor    int,
    vol_72h_before_anchor       numeric,
    buy_ratio_24h_before_anchor numeric,
    buy_ratio_72h_before_anchor numeric,

    -- ─── Velocity ratios ───
    vol_24h_pct_asof  numeric,
    vol_72h_pct_asof  numeric,

    -- ─── Phase B: Microstructure ───
    realized_vol_24h          numeric,
    realized_vol_72h          numeric,
    max_drawdown_24h_pct      numeric,
    vpin_proxy_lifetime       numeric,
    kyle_lambda_proxy         numeric,
    liquidity_quality_proxy   numeric,
    vol_24h_concentration     numeric,

    -- ─── Phase A: Wallet-level signal ───
    n_unique_takers_24h        int,
    n_unique_makers_24h        int,
    whale_taker_volume_24h     numeric,
    whale_taker_count_24h      int,
    retail_taker_avg_size_24h  numeric,
    whale_buy_pressure_24h     numeric,
    taker_hhi_proxy_24h        numeric,
    repeat_taker_ratio_24h     numeric,

    -- ─── Phase C: Event-level ───
    event_total_lifetime_volume  numeric,
    event_thinnest_market_vol    numeric,
    event_largest_market_share   numeric,
    event_yes_sum                numeric,
    event_arb_distance_yes       numeric,

    PRIMARY KEY (market_id, anchor_hours)
);

CREATE INDEX IF NOT EXISTS idx_live_ml_features_computed_at
    ON live_ml_features (computed_at);

COMMENT ON TABLE live_ml_features IS
    'Final 55-col feature matrix for ML detectors. Built every 30 min by app/jobs/live_ml_features_builder.py via app.ml.feature_kernel.build_features_sql(). Live anchor semantics: row only when anchor_ts = end_date - h <= now (no future leakage).';
