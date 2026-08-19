-- Замена B-tree на BRIN для oracle_snapshots.created_at
-- BRIN эффективнее для append-only time-series (1000x меньше, быстрее range scans)
-- B-tree на (market_id, created_at) остаётся для точечных lookups

DROP INDEX IF EXISTS idx_oracle_snapshots_created;

CREATE INDEX idx_oracle_snapshots_created_brin
    ON oracle_snapshots USING brin (created_at)
    WITH (pages_per_range = 64);
