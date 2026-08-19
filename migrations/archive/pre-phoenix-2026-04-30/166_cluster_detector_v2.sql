-- migrations/166_cluster_detector_v2.sql
-- ClusterDetector v2: новые колонки для многофакторного скоринга

ALTER TABLE wallet_clusters
  ADD COLUMN IF NOT EXISTS bf_temporal NUMERIC(8,4) DEFAULT 1.0,
  ADD COLUMN IF NOT EXISTS bf_entropy NUMERIC(8,4) DEFAULT 1.0,
  ADD COLUMN IF NOT EXISTS bf_position NUMERIC(8,4) DEFAULT 1.0,
  ADD COLUMN IF NOT EXISTS bf_graph NUMERIC(8,4) DEFAULT 1.0,
  ADD COLUMN IF NOT EXISTS market_ids TEXT[] DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'default',
  ADD COLUMN IF NOT EXISTS coordination_score_v2 NUMERIC(5,4) DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_wallet_clusters_active
  ON wallet_clusters(is_active) WHERE is_active = TRUE;

ALTER TABLE wallet_clusters
  ADD CONSTRAINT chk_coordination_score_v2_range
  CHECK (coordination_score_v2 >= 0 AND coordination_score_v2 <= 1);

-- Дедупликация перед UNIQUE
DELETE FROM wallet_clusters a USING wallet_clusters b
  WHERE a.id < b.id AND a.cluster_label = b.cluster_label;

ALTER TABLE wallet_clusters
  ADD CONSTRAINT uq_wallet_clusters_label UNIQUE (cluster_label);
