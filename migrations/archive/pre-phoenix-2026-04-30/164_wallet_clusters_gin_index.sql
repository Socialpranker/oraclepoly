-- 164: GIN индекс на wallet_addresses для быстрого поиска кошелька в кластерах
CREATE INDEX IF NOT EXISTS idx_wallet_clusters_addresses_gin ON wallet_clusters USING GIN (wallet_addresses);
