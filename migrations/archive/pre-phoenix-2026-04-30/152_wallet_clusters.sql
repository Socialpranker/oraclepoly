-- 152: Кластеры связанных кошельков
CREATE TABLE IF NOT EXISTS wallet_clusters (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cluster_label TEXT,
    detection_method TEXT DEFAULT 'dbscan_temporal',
    wallet_addresses TEXT[] DEFAULT '{}',
    total_volume NUMERIC DEFAULT 0,
    coordination_score NUMERIC(5,4) DEFAULT 0,
    detected_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wallet_clusters_detected ON wallet_clusters(detected_at DESC);
