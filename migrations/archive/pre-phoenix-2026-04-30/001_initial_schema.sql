-- Oracle Database Schema
-- Tables for whales, transactions, markets, news

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================
-- WHALES TABLE
-- Stores whale profiles (Smart Money wallets)
-- ============================================
CREATE TABLE IF NOT EXISTS whales (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    address TEXT UNIQUE NOT NULL,
    display_name TEXT,
    avatar_url TEXT,
    expert_weight NUMERIC(5, 4) DEFAULT 0.5, -- 0.0000 to 9.9999
    win_rate NUMERIC(5, 4) DEFAULT 0.5,
    total_volume NUMERIC(20, 6) DEFAULT 0, -- USDC (6 decimals) - NUMERIC for hardware precision
    category_expertise JSONB DEFAULT '{}', -- {"politics": 0.8, "crypto": 0.6}
    is_anomaly BOOLEAN DEFAULT FALSE, -- Insider detection flag
    first_seen_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for quick lookups by address
CREATE INDEX IF NOT EXISTS idx_whales_address ON whales(address);
CREATE INDEX IF NOT EXISTS idx_whales_is_anomaly ON whales(is_anomaly);

-- ============================================
-- TRANSACTIONS TABLE
-- Stores processed blockchain transactions
-- ============================================
CREATE TABLE IF NOT EXISTS transactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tx_hash TEXT UNIQUE NOT NULL, -- Idempotency key
    whale_id UUID REFERENCES whales(id),
    market_id UUID, -- Reference to markets table (nullable until mapped)
    direction TEXT CHECK (direction IN ('buy', 'sell')),
    outcome TEXT, -- "YES" or "NO"
    amount NUMERIC(20, 6) NOT NULL, -- USDC (6 decimals) - NUMERIC for hardware precision  
    price NUMERIC(10, 6), -- Price at time of transaction
    r2_archive_key TEXT, -- Path to full JSON in R2 Data Lake
    block_number BIGINT,
    block_timestamp TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_transactions_whale_id ON transactions(whale_id);
CREATE INDEX IF NOT EXISTS idx_transactions_market_id ON transactions(market_id);
CREATE INDEX IF NOT EXISTS idx_transactions_tx_hash ON transactions(tx_hash);
CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions(created_at DESC);

-- ============================================
-- MARKETS TABLE
-- Stores Polymarket market data
-- ============================================
CREATE TABLE IF NOT EXISTS markets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    polymarket_id TEXT UNIQUE NOT NULL, -- Original Polymarket ID
    question TEXT NOT NULL,
    description TEXT,
    category TEXT,
    icon_url TEXT,
    current_price NUMERIC(10, 6), -- Current YES price
    oracle_score NUMERIC(5, 4), -- Calculated Oracle Score (0-1)
    sentiment_score NUMERIC(5, 4), -- News sentiment (0-1)
    whale_confidence NUMERIC(5, 4), -- Weighted whale confidence (0-1)
    risk_score NUMERIC(5, 4), -- Black Swan risk (0-1)
    volume_24h NUMERIC(20, 6), -- USDC volume - NUMERIC for hardware precision
    end_date TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for active markets and categories
CREATE INDEX IF NOT EXISTS idx_markets_polymarket_id ON markets(polymarket_id);
CREATE INDEX IF NOT EXISTS idx_markets_is_active ON markets(is_active);
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets(category);

-- ============================================
-- NEWS TABLE
-- Stores scraped and processed news articles
-- ============================================
CREATE TABLE IF NOT EXISTS news (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source TEXT NOT NULL, -- "CNN", "Bloomberg", etc.
    title TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    summary TEXT,
    sentiment_score NUMERIC(5, 4), -- -1 to 1
    r2_archive_key TEXT, -- Path to full content in R2
    qdrant_point_id TEXT, -- Reference to vector in Qdrant
    published_at TIMESTAMPTZ,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for lookups
CREATE INDEX IF NOT EXISTS idx_news_source ON news(source);
CREATE INDEX IF NOT EXISTS idx_news_published_at ON news(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_url ON news(url);

-- ============================================
-- NEWS_MARKET_MAPPINGS TABLE
-- Links news articles to relevant markets
-- ============================================
CREATE TABLE IF NOT EXISTS news_market_mappings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    news_id UUID REFERENCES news(id) ON DELETE CASCADE,
    market_id UUID REFERENCES markets(id) ON DELETE CASCADE,
    confidence_score NUMERIC(5, 4) NOT NULL, -- Vector similarity score
    is_valid BOOLEAN DEFAULT TRUE, -- False if < 0.85 and failed LLM check
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(news_id, market_id)
);

CREATE INDEX IF NOT EXISTS idx_news_market_mappings_market_id ON news_market_mappings(market_id);

-- ============================================
-- BLACK_SWAN_SCENARIOS TABLE
-- Stores generated risk scenarios
-- ============================================
CREATE TABLE IF NOT EXISTS black_swan_scenarios (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    market_id UUID REFERENCES markets(id) ON DELETE CASCADE,
    scenario_title TEXT NOT NULL,
    trigger_event TEXT NOT NULL,
    probability_impact NUMERIC(5, 4), -- 0-1, how much price could drop
    reasoning TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_black_swan_market_id ON black_swan_scenarios(market_id);
CREATE INDEX IF NOT EXISTS idx_black_swan_is_active ON black_swan_scenarios(is_active);

-- ============================================
-- Enable Row Level Security (RLS)
-- ============================================
ALTER TABLE whales ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE markets ENABLE ROW LEVEL SECURITY;
ALTER TABLE news ENABLE ROW LEVEL SECURITY;
ALTER TABLE news_market_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE black_swan_scenarios ENABLE ROW LEVEL SECURITY;

-- Public read access for all tables (TMA users can view)
CREATE POLICY "Allow public read" ON whales FOR SELECT USING (true);
CREATE POLICY "Allow public read" ON transactions FOR SELECT USING (true);
CREATE POLICY "Allow public read" ON markets FOR SELECT USING (true);
CREATE POLICY "Allow public read" ON news FOR SELECT USING (true);
CREATE POLICY "Allow public read" ON news_market_mappings FOR SELECT USING (true);
CREATE POLICY "Allow public read" ON black_swan_scenarios FOR SELECT USING (true);

-- Service role can do everything (backend uses service key)
CREATE POLICY "Service role full access" ON whales FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "Service role full access" ON transactions FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "Service role full access" ON markets FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "Service role full access" ON news FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "Service role full access" ON news_market_mappings FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "Service role full access" ON black_swan_scenarios FOR ALL USING (auth.role() = 'service_role');

-- ============================================
-- Realtime (handled by pg_notify triggers, see migration 143)
-- ============================================
