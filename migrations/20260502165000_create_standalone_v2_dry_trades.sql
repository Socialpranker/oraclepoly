-- 20260502165000_create_standalone_v2_dry_trades.sql
-- Description: Dry trades для standalone V2 модели (paper trading).
--
-- Standalone V2 LGB видит каждый active market и генерит P(YES win at anchor).
-- Если |EV| > MIN_EV_PCT — создаём virtual trade ($5 stake, slippage 1%, fees 2%).
-- Cron resolve_dry_trades.py каждые 4h обновляет outcome когда market резолвится.
--
-- Эта таблица ОТДЕЛЬНА от pipeline_v2_shadow_predictions/execution_orders, потому что
-- standalone V2 — другая decision model (per-market, не per-detector-signal).

CREATE TABLE IF NOT EXISTS public.standalone_v2_dry_trades (
    id              BIGSERIAL PRIMARY KEY,
    prediction_id   BIGINT NOT NULL REFERENCES public.standalone_v2_predictions(id) ON DELETE CASCADE,
    market_id       TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    event_id        TEXT,
    anchor_hours    INTEGER NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('YES', 'NO')),
    -- Decision metadata
    predicted_p_yes DOUBLE PRECISION NOT NULL,
    market_yes_price DOUBLE PRECISION NOT NULL,
    ev_pct          DOUBLE PRECISION NOT NULL,  -- ev_yes_pct если YES, ev_no_pct если NO
    -- Trade
    entry_price     DOUBLE PRECISION NOT NULL,  -- side-aware price + slippage
    stake_usd       DOUBLE PRECISION NOT NULL DEFAULT 5.0,
    expected_payout DOUBLE PRECISION,           -- stake / entry_price (только если win)
    -- Outcome (backfilled когда market резолвится)
    yes_won         INTEGER CHECK (yes_won IN (0, 1)),
    is_winner       BOOLEAN,                    -- (side='YES' AND yes_won=1) OR (side='NO' AND yes_won=0)
    pnl_gross       DOUBLE PRECISION,           -- payout - stake
    pnl_net         DOUBLE PRECISION,           -- gross - fees
    fees_usd        DOUBLE PRECISION,
    resolved_at     TIMESTAMPTZ,
    -- Provenance
    category        TEXT,
    horizon_days    DOUBLE PRECISION,
    decision_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_version   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_standalone_v2_dry_trades_decision_at
    ON public.standalone_v2_dry_trades (decision_at DESC);
CREATE INDEX IF NOT EXISTS idx_standalone_v2_dry_trades_unresolved
    ON public.standalone_v2_dry_trades (resolved_at) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_standalone_v2_dry_trades_event_id
    ON public.standalone_v2_dry_trades (event_id) WHERE event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_standalone_v2_dry_trades_prediction
    ON public.standalone_v2_dry_trades (prediction_id);

COMMENT ON TABLE public.standalone_v2_dry_trades IS
    'Paper trades от standalone V2 LGB. EV-aware sizing. НЕ trade на реальные деньги.';
