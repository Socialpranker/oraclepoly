-- Migration: 120_create_events_table
-- Creates the events table for Polymarket event grouping

CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    polymarket_id TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    slug TEXT,
    description TEXT,
    category TEXT NOT NULL DEFAULT 'World & Economy',
    image_url TEXT,
    volume NUMERIC(20, 6) DEFAULT 0,
    volume_24h NUMERIC(20, 6) DEFAULT 0,
    liquidity NUMERIC(20, 6) DEFAULT 0,
    start_date TIMESTAMPTZ,
    end_date TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE,
    markets_count INTEGER DEFAULT 0,
    tags JSONB DEFAULT '[]',
    competitive NUMERIC(5, 4),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_events_category ON events(category);
CREATE INDEX idx_events_active ON events(is_active) WHERE is_active = TRUE;
CREATE INDEX idx_events_volume ON events(volume_24h DESC);
CREATE INDEX idx_events_slug ON events(slug);
