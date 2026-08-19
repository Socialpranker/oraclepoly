-- 154: Smart Money колонки в whales
ALTER TABLE whales ADD COLUMN IF NOT EXISTS insider_score NUMERIC(5,4) DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS fresh_wallet_risk NUMERIC(5,4) DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS cluster_id UUID REFERENCES wallet_clusters(id) ON DELETE SET NULL;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS smart_money_wallet_id UUID REFERENCES smart_money_wallets(id) ON DELETE SET NULL;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS funding_depth INTEGER DEFAULT 0;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS sector_win_rates JSONB DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_whales_insider_score ON whales(insider_score DESC) WHERE insider_score > 0;
CREATE INDEX IF NOT EXISTS idx_whales_cluster ON whales(cluster_id) WHERE cluster_id IS NOT NULL;

-- Проставить smart_money_wallet_id для уже существующих китов
UPDATE whales w SET smart_money_wallet_id = s.id
FROM smart_money_wallets s
WHERE LOWER(w.address) = LOWER(s.address);
