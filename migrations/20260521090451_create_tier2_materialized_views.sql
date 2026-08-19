-- 20260521090451_create_tier2_materialized_views.sql
-- Description: 5 Materialized Views для tier-2 интерпретации (Layer 3 архитектуры).
--
-- Архитектура (docs/data_audit_2026-05-21.md + session-2026-05-21):
--   Layer 1 (ingest)         — cron jobs, WSS, hooks
--   Layer 2 (hot storage)    — Postgres tables
-->  Layer 3 (analysis-ready) — эти MV + Parquet exports
--   Layer 4 (interpretation) — Jupyter, dashboard, TG digest
--
-- Каждый MV отвечает на одну исследовательскую гипотезу. Refresh hourly.
--
-- Зависимости:
--   - markets.uma_resolution_status, uma_resolution_statuses (миграция 20260521083820)
--   - manifold_bets (миграция 20260521083821)
--   - orderbook_event_snapshots (миграция 20260521083822)
--   - manifold_markets, manifold_price_snapshots (миграция 20260520171823)
--   - venue_market_mappings (Phoenix schema)

-- ─────────────────────────────────────────────────────────────────────
-- MV 1: UMA dispute timeline (гипотеза: ≥2 disputes = unreliable resolution)
-- ─────────────────────────────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_uma_dispute_timeline AS
SELECT
    m.id                              AS market_id,
    m.condition_id,
    m.question,
    m.category,
    m.uma_resolution_status           AS current_status,
    m.uma_resolution_statuses,
    COALESCE(jsonb_array_length(m.uma_resolution_statuses), 0) AS status_changes,
    -- Counts per state
    (SELECT count(*) FROM jsonb_array_elements_text(m.uma_resolution_statuses) elem
     WHERE elem = 'disputed') AS dispute_count,
    (SELECT count(*) FROM jsonb_array_elements_text(m.uma_resolution_statuses) elem
     WHERE elem = 'proposed') AS proposed_count,
    m.uma_bond,
    m.uma_reward,
    m.current_price,
    m.resolved,
    m.winner,
    m.end_date,
    m.updated_at
FROM markets m
WHERE m.uma_resolution_statuses IS NOT NULL
   OR m.uma_resolution_status IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_uma_dispute_market
    ON mv_uma_dispute_timeline (market_id);

CREATE INDEX IF NOT EXISTS idx_mv_uma_dispute_count
    ON mv_uma_dispute_timeline (dispute_count DESC)
    WHERE dispute_count >= 1;

CREATE INDEX IF NOT EXISTS idx_mv_uma_current_status
    ON mv_uma_dispute_timeline (current_status)
    WHERE current_status IN ('proposed', 'disputed');

COMMENT ON MATERIALIZED VIEW mv_uma_dispute_timeline IS
    'Per-market UMA dispute state + history. Refresh hourly. Гипотеза: disputed-cohort markets имеют worse calibration.';

-- ─────────────────────────────────────────────────────────────────────
-- MV 2: Manifold ↔ Polymarket divergence pairs (lead-lag гипотеза)
-- ─────────────────────────────────────────────────────────────────────
-- Pairs где есть mapping в venue_market_mappings + текущие probability обеих сторон.
-- Lead-lag = |poly_prob - manifold_prob|; positive divergence_hours = Polymarket позже двинулся.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_manifold_polymarket_pairs AS
WITH polymarket_mapping AS (
    SELECT
        vmm_poly.canonical_event_id,
        vmm_poly.venue_market_id      AS poly_market_uuid
    FROM venue_market_mappings vmm_poly
    WHERE vmm_poly.venue = 'polymarket'
),
manifold_mapping AS (
    SELECT
        vmm_man.canonical_event_id,
        vmm_man.venue_market_id      AS manifold_market_id
    FROM venue_market_mappings vmm_man
    WHERE vmm_man.venue = 'manifold'
),
last_manifold_bet AS (
    SELECT
        manifold_market_id,
        max(created_time) AS last_bet_time,
        count(*) AS bet_count_7d
    FROM manifold_bets
    WHERE created_time > now() - interval '7 days'
    GROUP BY manifold_market_id
)
SELECT
    pm.canonical_event_id,
    p.id                              AS polymarket_market_id,
    p.condition_id                    AS polymarket_condition_id,
    p.question                        AS polymarket_question,
    p.current_price                   AS polymarket_yes_prob,
    p.volume_24h                      AS polymarket_volume_24h,
    mm.manifold_market_id,
    mfm.question                      AS manifold_question,
    mfm.probability                   AS manifold_yes_prob,
    mfm.volume_24h                    AS manifold_volume_24h,
    -- Divergence (positive = Polymarket dearer)
    (p.current_price - mfm.probability) AS divergence_pp,
    abs(p.current_price - mfm.probability) AS abs_divergence_pp,
    lmb.last_bet_time                 AS last_manifold_bet,
    lmb.bet_count_7d                  AS manifold_bets_7d,
    p.updated_at                      AS polymarket_updated_at,
    mfm.last_updated_time             AS manifold_updated_at
FROM polymarket_mapping pm
INNER JOIN manifold_mapping mm USING (canonical_event_id)
INNER JOIN markets p
    ON p.id::text = pm.poly_market_uuid
   AND p.is_active = true
INNER JOIN manifold_markets mfm
    ON mfm.id = mm.manifold_market_id
   AND mfm.is_resolved = false
LEFT JOIN last_manifold_bet lmb
    ON lmb.manifold_market_id = mm.manifold_market_id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_manifold_poly_pair
    ON mv_manifold_polymarket_pairs (polymarket_market_id, manifold_market_id);

CREATE INDEX IF NOT EXISTS idx_mv_manifold_poly_divergence
    ON mv_manifold_polymarket_pairs (abs_divergence_pp DESC);

COMMENT ON MATERIALIZED VIEW mv_manifold_polymarket_pairs IS
    'Per-pair cross-venue divergence + Manifold bet recency. Refresh hourly. Гипотеза: Manifold lead-lag 4-12h.';

-- ─────────────────────────────────────────────────────────────────────
-- MV 3: Whale shock recovery (orderbook reaction time)
-- ─────────────────────────────────────────────────────────────────────
-- Per event_id из orderbook_event_snapshots: depth pre-shock, time to recovery, post-shock movement.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_whale_shock_recovery AS
WITH event_summary AS (
    SELECT
        event_id,
        trigger_type,
        market_id,
        condition_id,
        min(captured_at)              AS event_start,
        max(captured_at)              AS event_end,
        max(captured_at) - min(captured_at) AS window_span,
        count(*)                      AS n_snapshots,
        -- Depth at event start (smallest t_offset_ms ≥ 0)
        min(case when t_offset_ms >= 0 then depth_bid_usd + depth_ask_usd end) AS depth_at_start,
        -- Depth at event end (largest t_offset_ms)
        max(case when t_offset_ms >= 50000 then depth_bid_usd + depth_ask_usd end) AS depth_at_50s,
        -- Mid price at start vs end (price impact)
        min(case when t_offset_ms >= 0 then midpoint end) AS midpoint_at_start,
        max(case when t_offset_ms >= 50000 then midpoint end) AS midpoint_at_50s,
        -- Spread evolution
        min(case when t_offset_ms >= 0 then spread end) AS spread_at_start,
        max(case when t_offset_ms >= 50000 then spread end) AS spread_at_50s,
        -- Trigger meta из первого snapshot
        (array_agg(trigger_meta ORDER BY t_offset_ms ASC))[1] AS trigger_meta
    FROM orderbook_event_snapshots
    GROUP BY event_id, trigger_type, market_id, condition_id
)
SELECT
    es.event_id,
    es.trigger_type,
    es.market_id,
    es.condition_id,
    m.question,
    m.category,
    es.event_start,
    es.event_end,
    es.window_span,
    es.n_snapshots,
    es.depth_at_start,
    es.depth_at_50s,
    -- Recovery ratio: 1.0 = полное восстановление за 50s
    CASE WHEN es.depth_at_start > 0
         THEN es.depth_at_50s / es.depth_at_start
         ELSE NULL END                AS depth_recovery_ratio,
    es.midpoint_at_start,
    es.midpoint_at_50s,
    (es.midpoint_at_50s - es.midpoint_at_start) AS price_impact_50s,
    es.spread_at_start,
    es.spread_at_50s,
    es.trigger_meta
FROM event_summary es
LEFT JOIN markets m ON m.id = es.market_id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_whale_shock_event
    ON mv_whale_shock_recovery (event_id);

CREATE INDEX IF NOT EXISTS idx_mv_whale_shock_trigger_time
    ON mv_whale_shock_recovery (trigger_type, event_start DESC);

CREATE INDEX IF NOT EXISTS idx_mv_whale_shock_recovery_ratio
    ON mv_whale_shock_recovery (depth_recovery_ratio);

COMMENT ON MATERIALIZED VIEW mv_whale_shock_recovery IS
    'Per-event orderbook depth recovery + price impact. Refresh hourly. Гипотеза: slow recovery (>30s) = momentum tail.';

-- ─────────────────────────────────────────────────────────────────────
-- MV 4: Term-structure momentum performance (D41 enhancement)
-- ─────────────────────────────────────────────────────────────────────
-- Markets с измеренным change за разные горизонты × ROI после резолюции (когда resolved).
-- Если markets ещё active — show current state без outcome.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_term_structure_momentum AS
SELECT
    m.id                              AS market_id,
    m.condition_id,
    m.category,
    m.question,
    m.current_price                   AS yes_prob_current,
    m.one_hour_price_change,
    m.one_day_price_change,
    m.one_week_price_change,
    m.one_month_price_change,
    m.one_year_price_change,
    m.volume_24h,
    -- Classification по направлению short-term momentum
    CASE
        WHEN m.one_hour_price_change > 0.05 THEN 'strong_up_1h'
        WHEN m.one_hour_price_change > 0.01 THEN 'mild_up_1h'
        WHEN m.one_hour_price_change < -0.05 THEN 'strong_down_1h'
        WHEN m.one_hour_price_change < -0.01 THEN 'mild_down_1h'
        ELSE 'flat'
    END                               AS momentum_regime_1h,
    m.resolved,
    m.winner,
    -- Outcome encoded as +1 (YES won) / -1 (NO won) / 0 (active)
    CASE
        WHEN m.winner = 'yes' THEN 1
        WHEN m.winner = 'no' THEN -1
        ELSE 0
    END                               AS outcome_signed,
    m.end_date,
    m.updated_at
FROM markets m
WHERE m.one_day_price_change IS NOT NULL
   OR m.one_hour_price_change IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_term_structure_market
    ON mv_term_structure_momentum (market_id);

CREATE INDEX IF NOT EXISTS idx_mv_term_structure_regime
    ON mv_term_structure_momentum (momentum_regime_1h, category);

COMMENT ON MATERIALIZED VIEW mv_term_structure_momentum IS
    'Markets × term-structure momentum × outcome. Refresh hourly. D41 enhancement + backtest input.';

-- ─────────────────────────────────────────────────────────────────────
-- MV 5: CYOM/restricted cohort stats (filter check)
-- ─────────────────────────────────────────────────────────────────────
-- Group markets по cohort flag и сравниваем win-rate / ROI.
-- Polymarket cyom-markets подозреваются на mispricing.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_cohort_stats AS
SELECT
    coalesce(m.category, 'unknown')   AS category,
    coalesce(m.cyom, false)           AS is_cyom,
    coalesce(m.restricted, false)     AS is_restricted,
    coalesce(m.neg_risk_augmented, false) AS is_neg_risk_aug,
    count(*)                          AS n_markets,
    count(*) FILTER (WHERE m.resolved) AS n_resolved,
    count(*) FILTER (WHERE m.winner = 'yes') AS n_yes_won,
    count(*) FILTER (WHERE m.winner = 'no') AS n_no_won,
    -- Win rate (для resolved)
    case when count(*) FILTER (WHERE m.resolved) > 0
         then (count(*) FILTER (WHERE m.winner = 'yes'))::float / count(*) FILTER (WHERE m.resolved)
         else null end                AS yes_win_rate,
    -- Price distribution
    avg(m.current_price)              AS avg_current_price,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY m.current_price) AS median_current_price,
    -- Volume distribution
    avg(m.volume_24h)                 AS avg_volume_24h,
    sum(m.volume_24h)                 AS total_volume_24h
FROM markets m
WHERE m.cyom IS NOT NULL OR m.restricted IS NOT NULL
GROUP BY 1, 2, 3, 4;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_cohort_combo
    ON mv_cohort_stats (category, is_cyom, is_restricted, is_neg_risk_aug);

COMMENT ON MATERIALIZED VIEW mv_cohort_stats IS
    'Per-cohort aggregates (category × cyom × restricted × neg_risk_augmented). Refresh hourly. Гипотеза: cyom underpriced.';

-- ─────────────────────────────────────────────────────────────────────
-- Rollback:
-- ─────────────────────────────────────────────────────────────────────
-- DROP MATERIALIZED VIEW IF EXISTS mv_uma_dispute_timeline;
-- DROP MATERIALIZED VIEW IF EXISTS mv_manifold_polymarket_pairs;
-- DROP MATERIALIZED VIEW IF EXISTS mv_whale_shock_recovery;
-- DROP MATERIALIZED VIEW IF EXISTS mv_term_structure_momentum;
-- DROP MATERIALIZED VIEW IF EXISTS mv_cohort_stats;
