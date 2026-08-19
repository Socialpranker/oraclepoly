-- 20260501120001_create_detector_state.sql
-- Description: Per-detector live/shadow stage tracking. Replaces strategy_state for V2 pipeline.
-- Promotion is manual (UPDATE detector_state SET stage='live' WHERE detector=...).

CREATE TABLE IF NOT EXISTS public.detector_state (
    detector                text PRIMARY KEY,
    stage                   text NOT NULL DEFAULT 'shadow' CHECK (stage IN ('shadow', 'live', 'paused')),
    paused_reason           text,
    promoted_at             timestamptz,
    n_resolved              integer NOT NULL DEFAULT 0,
    wr_30d                  numeric(5,2),
    pnl_total_usd           numeric(10,2) NOT NULL DEFAULT 0,
    drawdown_pct            numeric(5,2) NOT NULL DEFAULT 0,
    fractional_kelly_today  numeric(4,3) NOT NULL DEFAULT 0.10,
    updated_at              timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.detector_state (detector, stage) VALUES
    ('D1_fade_whale',                'shadow'),
    ('D3_cross_mkt_arb',             'shadow'),
    ('D4_small_market_fade',         'shadow'),
    ('D5_long_horizon_extreme_fade', 'shadow'),
    ('D6_retail_bullish_overshoot',  'shadow'),
    ('D6b_long_horizon',             'shadow')
ON CONFLICT (detector) DO NOTHING;
