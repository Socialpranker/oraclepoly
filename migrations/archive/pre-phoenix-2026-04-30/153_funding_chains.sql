-- 153: Цепочки финансирования кошельков
CREATE TABLE IF NOT EXISTS funding_chains (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_address TEXT NOT NULL,
    funder_address TEXT NOT NULL,
    chain_depth INTEGER DEFAULT 1,
    amount NUMERIC DEFAULT 0,
    detected_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_funding_chains_target ON funding_chains(target_address);
CREATE INDEX IF NOT EXISTS idx_funding_chains_funder ON funding_chains(funder_address);
CREATE UNIQUE INDEX IF NOT EXISTS idx_funding_chains_pair ON funding_chains(target_address, funder_address);
