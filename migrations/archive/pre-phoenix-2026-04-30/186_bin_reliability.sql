-- 186: Bin Reliability — метрики калибровки по бинам oracle_score

CREATE TABLE IF NOT EXISTS bin_reliability (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bin_start NUMERIC(3,2) NOT NULL,
    bin_end NUMERIC(3,2) NOT NULL,
    bin_label VARCHAR(10) NOT NULL,
    sample_count INT NOT NULL DEFAULT 0,
    avg_predicted NUMERIC(6,4),
    actual_win_rate NUMERIC(6,4),
    gap NUMERIC(6,4),
    abs_gap NUMERIC(6,4),
    is_whitelisted BOOLEAN NOT NULL DEFAULT false,
    confidence_lower NUMERIC(6,4),
    confidence_upper NUMERIC(6,4),
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(bin_label, computed_at)
);

CREATE INDEX IF NOT EXISTS idx_bin_reliability_latest
    ON bin_reliability(computed_at DESC);

CREATE INDEX IF NOT EXISTS idx_bin_reliability_whitelisted
    ON bin_reliability(is_whitelisted) WHERE is_whitelisted = true;
