-- 139_political_tables.sql
-- Political Intelligence Tab: справочник конгрессменов, сделки, голосования, лоббизм, корреляции с рынками

-- 1. Справочник конгрессменов
CREATE TABLE IF NOT EXISTS congress_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    party TEXT NOT NULL,
    chamber TEXT NOT NULL,
    state TEXT,
    committees JSONB DEFAULT '[]',
    photo_url TEXT,
    bioguide_id TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_congress_members_party ON congress_members(party);
CREATE INDEX IF NOT EXISTS idx_congress_members_chamber ON congress_members(chamber);

-- 2. Сделки (STOCK Act disclosures)
CREATE TABLE IF NOT EXISTS congress_trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    member_id UUID NOT NULL REFERENCES congress_members(id),
    ticker TEXT,
    asset_name TEXT NOT NULL,
    trade_type TEXT NOT NULL,
    amount_range TEXT NOT NULL,
    trade_date DATE NOT NULL,
    disclosure_date DATE NOT NULL,
    is_option BOOLEAN DEFAULT FALSE,
    source_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(member_id, asset_name, trade_date, trade_type, amount_range)
);

CREATE INDEX IF NOT EXISTS idx_congress_trades_member ON congress_trades(member_id);
CREATE INDEX IF NOT EXISTS idx_congress_trades_date ON congress_trades(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_congress_trades_ticker ON congress_trades(ticker);

-- 3. Голосования
CREATE TABLE IF NOT EXISTS congress_votes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_id TEXT NOT NULL,
    bill_title TEXT NOT NULL,
    vote_date DATE NOT NULL,
    chamber TEXT NOT NULL,
    result TEXT NOT NULL,
    party_split JSONB,
    member_votes JSONB DEFAULT '{}',
    source_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(bill_id, chamber, vote_date)
);

CREATE INDEX IF NOT EXISTS idx_congress_votes_date ON congress_votes(vote_date DESC);
CREATE INDEX IF NOT EXISTS idx_congress_votes_bill ON congress_votes(bill_id);

-- 4. Лоббизм / донаты
CREATE TABLE IF NOT EXISTS congress_lobbying (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    member_id UUID NOT NULL REFERENCES congress_members(id),
    donor_name TEXT NOT NULL,
    donor_industry TEXT,
    amount NUMERIC NOT NULL,
    cycle TEXT NOT NULL,
    source_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(member_id, donor_name, cycle)
);

CREATE INDEX IF NOT EXISTS idx_congress_lobbying_member ON congress_lobbying(member_id);
CREATE INDEX IF NOT EXISTS idx_congress_lobbying_industry ON congress_lobbying(donor_industry);

-- 5. Корреляции с рынками Polymarket
CREATE TABLE IF NOT EXISTS political_market_correlations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL,
    source_id UUID NOT NULL,
    market_id UUID NOT NULL REFERENCES markets(id),
    confidence_score FLOAT NOT NULL,
    correlation_type TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_type, source_id, market_id)
);

CREATE INDEX IF NOT EXISTS idx_political_correlations_market ON political_market_correlations(market_id);
CREATE INDEX IF NOT EXISTS idx_political_correlations_source ON political_market_correlations(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_political_correlations_type ON political_market_correlations(correlation_type);
