-- Migration 133: RPC function to get user predictions with snapshot + market data

CREATE OR REPLACE FUNCTION get_user_predictions(
    p_user_id UUID,
    p_status TEXT DEFAULT 'all',
    p_limit INT DEFAULT 20,
    p_offset INT DEFAULT 0
)
RETURNS TABLE (
    vote_id UUID,
    prediction TEXT,
    confidence INT,
    rewarded BOOLEAN,
    xp_earned INT,
    vote_created_at TIMESTAMPTZ,
    oracle_score NUMERIC,
    current_price NUMERIC,
    sentiment_score NUMERIC,
    whale_confidence NUMERIC,
    risk_score NUMERIC,
    divergence_signal TEXT,
    ai_hint TEXT,
    unlocked_tabs TEXT[],
    tab_insights JSONB,
    user_notes TEXT,
    outcome TEXT,
    price_at_resolve NUMERIC,
    resolved_at TIMESTAMPTZ,
    market_id TEXT,
    question TEXT,
    category TEXT,
    icon_url TEXT,
    end_date TIMESTAMPTZ,
    is_active BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    SELECT
        cv.id AS vote_id,
        cv.prediction,
        cv.confidence,
        cv.rewarded,
        cv.xp_earned,
        cv.created_at AS vote_created_at,
        vs.oracle_score,
        vs.current_price,
        vs.sentiment_score,
        vs.whale_confidence,
        vs.risk_score,
        vs.divergence_signal,
        vs.ai_hint,
        vs.unlocked_tabs,
        vs.tab_insights,
        vs.user_notes,
        vs.outcome,
        vs.price_at_resolve,
        vs.resolved_at,
        cv.market_id,
        m.question,
        m.category,
        m.icon_url,
        m.end_date,
        m.is_active
    FROM consensus_votes cv
    LEFT JOIN vote_snapshots vs ON vs.vote_id = cv.id
    LEFT JOIN markets m ON m.id::text = cv.market_id OR m.polymarket_id = cv.market_id
    WHERE cv.user_id = p_user_id
      AND (p_status = 'all'
           OR (p_status = 'active' AND (vs.outcome IS NULL OR vs.outcome = 'pending'))
           OR (p_status = 'resolved' AND vs.outcome IN ('win', 'loss', 'void'))
      )
    ORDER BY cv.created_at DESC
    LIMIT p_limit
    OFFSET p_offset;
END;
$$;
