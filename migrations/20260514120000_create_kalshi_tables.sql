-- Kalshi integration Phase 1 — tables
-- Source: research/kalshi-integration-2026-05/prd.md
-- Date: 2026-05-14

-- ─────────────────────────────────────────────────────────────────────────
-- kalshi_markets — mirror к нашей markets table, но Kalshi-side.
-- Только активные + recently closed (для post-resolve проверки arb closure).
-- Cron `kalshi-market-sync` upsert каждые 30 мин.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.kalshi_markets (
    ticker                  text PRIMARY KEY,        -- Kalshi market ticker (unique)
    event_ticker            text NOT NULL,           -- группа маркетов в одном событии
    series_ticker           text,                    -- broader series, для группировки
    title                   text NOT NULL,
    subtitle                text,
    category                text,
    yes_bid_cents           smallint,                -- 1-99
    yes_ask_cents           smallint,
    no_bid_cents            smallint,
    no_ask_cents            smallint,
    last_price_cents        smallint,
    volume_24h_contracts    integer,
    open_interest           integer,
    open_time               timestamptz,
    close_time              timestamptz,
    expiration_time         timestamptz,
    is_active               boolean DEFAULT true,
    raw                     jsonb,                   -- full Kalshi response для debug
    created_at              timestamptz DEFAULT now(),
    updated_at              timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_kalshi_markets_event ON public.kalshi_markets (event_ticker);
CREATE INDEX IF NOT EXISTS idx_kalshi_markets_category ON public.kalshi_markets (category) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_kalshi_markets_close_time ON public.kalshi_markets (close_time DESC) WHERE is_active = true;

COMMENT ON TABLE public.kalshi_markets IS 'Kalshi side mirror — synced via kalshi-market-sync cron каждые 30 min';

-- ─────────────────────────────────────────────────────────────────────────
-- kalshi_orderbook_snapshots — time-series для будущего backtest.
-- Phase 1.2: capture каждые 5 мин для tracked tickers (из market_pair_matches).
-- Storage: ~100 markets × 12 snapshots/hour × 24 = 28K rows/day = manageable.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.kalshi_orderbook_snapshots (
    ticker          text NOT NULL,
    captured_at     timestamptz NOT NULL,
    yes_bid_cents   smallint,
    yes_bid_size    integer,
    yes_ask_cents   smallint,
    yes_ask_size    integer,
    no_bid_cents    smallint,
    no_bid_size     integer,
    no_ask_cents    smallint,
    no_ask_size     integer,
    PRIMARY KEY (ticker, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_kalshi_orderbook_captured ON public.kalshi_orderbook_snapshots (captured_at DESC);

COMMENT ON TABLE public.kalshi_orderbook_snapshots IS 'Time-series Kalshi orderbook for tracked tickers — written by kalshi-orderbook-sync cron (Phase 1.2)';

-- ─────────────────────────────────────────────────────────────────────────
-- market_pair_matches — сопоставление Polymarket ↔ Kalshi pairs.
-- Auto-matched через embeddings (Phase 1.1). Inverse_side для случаев когда
-- семантика противоположна (Poly "X wins" vs Kalshi "X loses").
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.market_pair_matches (
    polymarket_market_id    text NOT NULL,           -- из markets.polymarket_id
    kalshi_ticker           text NOT NULL,           -- FK soft на kalshi_markets.ticker
    similarity_score        numeric(5,4) NOT NULL,   -- cosine 0..1
    auto_matched            boolean DEFAULT true,
    confirmed_by            text,                    -- user_id / TG-callback если manual
    inverse_side            boolean DEFAULT false,   -- true если Poly YES = Kalshi NO
    notes                   text,
    created_at              timestamptz DEFAULT now(),
    expired_at              timestamptz,             -- когда один из маркетов закрылся
    PRIMARY KEY (polymarket_market_id, kalshi_ticker)
);

CREATE INDEX IF NOT EXISTS idx_market_pair_active
    ON public.market_pair_matches (polymarket_market_id)
    WHERE expired_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_market_pair_kalshi_active
    ON public.market_pair_matches (kalshi_ticker)
    WHERE expired_at IS NULL;

COMMENT ON TABLE public.market_pair_matches IS 'Polymarket ↔ Kalshi pair matches — populated by matcher (Phase 1.1)';
