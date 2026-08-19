-- 20260503202532_create_auto_edge_discoveries.sql
-- Auto-generated edge candidates from weekly scanner (scripts/auto_edge_scanner.py).
-- Plan: ~/.claude/plans/wiggly-nibbling-stardust.md
-- Each row = one (feature, threshold, zone, anchor, category, side) cell that
-- was evaluated by the weekly scan. Cells with status='novel' AND verdict in
-- ('PASS','MARGINAL') trigger Telegram alerts.

CREATE TABLE IF NOT EXISTS public.auto_edge_discoveries (
    id BIGSERIAL PRIMARY KEY,
    scan_run_id UUID NOT NULL,
    scan_date DATE NOT NULL,

    -- Hypothesis
    feature TEXT NOT NULL,
    threshold_op TEXT NOT NULL,
    threshold_value DOUBLE PRECISION NOT NULL,
    zone TEXT NOT NULL,
    zone_lo DOUBLE PRECISION NOT NULL,
    zone_hi DOUBLE PRECISION NOT NULL,
    anchor_hours INT NOT NULL,
    category TEXT,
    side TEXT NOT NULL,

    -- Stats
    n_signals INT NOT NULL,
    n_unique_markets INT NOT NULL,
    win_rate DOUBLE PRECISION NOT NULL,
    avg_cost DOUBLE PRECISION NOT NULL,
    net_roi DOUBLE PRECISION NOT NULL,
    mirror_net_roi DOUBLE PRECISION NOT NULL,
    mirror_diff_pp DOUBLE PRECISION NOT NULL,
    year_2025_net_roi DOUBLE PRECISION,
    year_2026_net_roi DOUBLE PRECISION,
    year_consistency_score DOUBLE PRECISION,
    daily_signals DOUBLE PRECISION NOT NULL,
    daily_raw_pnl_at_5k DOUBLE PRECISION NOT NULL,
    daily_honest_pnl_at_5k DOUBLE PRECISION NOT NULL,

    -- Multiple-testing control
    p_value DOUBLE PRECISION,
    bh_adjusted_p DOUBLE PRECISION,

    -- Composite ranking
    rank_score DOUBLE PRECISION NOT NULL,

    -- Dedup overlap pcts vs each active detector
    overlap_pct_d3 DOUBLE PRECISION DEFAULT 0,
    overlap_pct_d5 DOUBLE PRECISION DEFAULT 0,
    overlap_pct_d7b DOUBLE PRECISION DEFAULT 0,
    overlap_pct_d10b DOUBLE PRECISION DEFAULT 0,
    overlap_pct_d13 DOUBLE PRECISION DEFAULT 0,
    overlap_pct_d14 DOUBLE PRECISION DEFAULT 0,
    overlap_pct_max DOUBLE PRECISION NOT NULL,

    -- Lifecycle
    status TEXT NOT NULL DEFAULT 'pending',
    verdict TEXT NOT NULL,
    cell_signature TEXT NOT NULL,
    first_seen_scan_date DATE NOT NULL,
    n_times_seen INT NOT NULL DEFAULT 1,
    notified_at TIMESTAMPTZ,
    promoted_to TEXT,
    notes TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT auto_edge_status_check
        CHECK (status IN ('novel','subset','killed','promoted','dismissed','pending','partial_overlap'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_edge_signature_run
    ON public.auto_edge_discoveries (cell_signature, scan_run_id);

CREATE INDEX IF NOT EXISTS idx_auto_edge_status_date
    ON public.auto_edge_discoveries (status, scan_date DESC);

CREATE INDEX IF NOT EXISTS idx_auto_edge_signature
    ON public.auto_edge_discoveries (cell_signature);

CREATE INDEX IF NOT EXISTS idx_auto_edge_rank
    ON public.auto_edge_discoveries (scan_date DESC, rank_score DESC);

COMMENT ON TABLE public.auto_edge_discoveries IS
    'Weekly edge-candidate scan results. Populated by scripts/auto_edge_scanner.py Sun 03:00 UTC.';

COMMENT ON COLUMN public.auto_edge_discoveries.cell_signature IS
    'sha1 of normalized (feature|op|value|zone|anchor|category|side) - stable hash for cross-week dedup.';
