-- Migration 111: Watchlist — save markets for later

CREATE TABLE IF NOT EXISTS user_watchlist (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    market_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, market_id)
);

CREATE INDEX IF NOT EXISTS idx_user_watchlist_user ON user_watchlist(user_id);
