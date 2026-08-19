-- migrations/173_market_comments.sql
CREATE TABLE IF NOT EXISTS market_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id UUID NOT NULL REFERENCES markets(id),
    comment_id TEXT NOT NULL,
    author_address TEXT,
    body TEXT NOT NULL,
    is_holder BOOLEAN DEFAULT FALSE,
    sentiment_score NUMERIC(4, 3),
    created_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(comment_id)
);

CREATE INDEX IF NOT EXISTS idx_market_comments_market ON market_comments(market_id);
CREATE INDEX IF NOT EXISTS idx_market_comments_holder ON market_comments(market_id, is_holder) WHERE is_holder = TRUE;
