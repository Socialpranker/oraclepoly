-- 20260521083822_create_orderbook_event_snapshots.sql
-- Description: Event-shock orderbook snapshots для D36/D37 + новых microstructure детекторов.
--
-- Аудит 2026-05-21 (docs/data_audit_2026-05-21.md C.6 gap #5, приоритет 4):
-- Polymarket WSS depth — TTL 60s в Redis, historical depth теряется.
-- Никто из OSS не сохраняет историю стакана (см. наш урок с MM asymmetry).
--
-- Принцип: НЕ continuous persistence (грабли с depth_total scalar), а
-- event-triggered snapshots в окне ±60s вокруг shock events:
--   T1: whale trade ≥$50k
--   T2: spread spike +200% за 30s
--   T3: UMA dispute event (umaResolutionStatuses change)
--   T4: comment burst (out of scope, comments writer не делаем)
--   T5: context_description diff (новый context_updated_at)
--   T6: detector emit (опционально, anti self-validate)
--   T7: price movement >5% за 5min
--
-- Phase 1 (этот PR): только T1 (whale_trade ≥$50k). Расширения T2-T7 — отдельно.
--
-- Объём: ~50 whale trades/день × 240 rows (120s × 2 tokens) = 12K rows/день =
-- ~7 MB/день в Postgres, или ~100 MB/мес.
-- Retention 14 дней → ~100 MB hot footprint.
-- Cold archive (Parquet) ≈ 5× compression → ~20 MB/мес ≈ 240 MB/год.

CREATE TABLE IF NOT EXISTS public.orderbook_event_snapshots (
    -- Event identification
    event_id        uuid NOT NULL,              -- группирует все snapshots одного триггера
    trigger_type    text NOT NULL,              -- 'whale_trade' | 'spread_spike' | 'uma_dispute' | ...
    trigger_meta    jsonb,                      -- {trade_size, dispute_status, ...} специфично для триггера

    -- Market context
    market_id       uuid NOT NULL,
    condition_id    text NOT NULL,
    token_id        text NOT NULL,              -- один token = один ladder (YES или NO)

    -- Time
    captured_at     timestamptz NOT NULL,
    t_offset_ms     integer NOT NULL,           -- (-60000..+60000) от event_t0

    -- Top-of-book derived
    best_bid        numeric(8, 6),
    best_ask        numeric(8, 6),
    midpoint        numeric(8, 6),
    spread          numeric(8, 6),

    -- Full ladder (top-10 each side)
    bids_ladder     jsonb NOT NULL,             -- [{p, s}, ...] top-10
    asks_ladder     jsonb NOT NULL,             -- [{p, s}, ...] top-10

    -- Depth aggregates
    depth_bid_usd   numeric(20, 6),
    depth_ask_usd   numeric(20, 6),

    PRIMARY KEY (event_id, token_id, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_obes_market_time
    ON public.orderbook_event_snapshots (market_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_obes_trigger_time
    ON public.orderbook_event_snapshots (trigger_type, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_obes_event
    ON public.orderbook_event_snapshots (event_id);

COMMENT ON TABLE public.orderbook_event_snapshots IS
    'Event-triggered orderbook snapshots ±60s вокруг shock events. Retention 14d hot, потом → Parquet archive.';

-- Rollback:
-- DROP TABLE IF EXISTS public.orderbook_event_snapshots;
