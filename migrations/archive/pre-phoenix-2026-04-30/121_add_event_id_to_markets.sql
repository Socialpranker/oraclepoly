-- Migration: 121_add_event_id_to_markets
-- Links markets to parent events via FK

ALTER TABLE markets ADD COLUMN IF NOT EXISTS event_id UUID REFERENCES events(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_markets_event_id ON markets(event_id);
