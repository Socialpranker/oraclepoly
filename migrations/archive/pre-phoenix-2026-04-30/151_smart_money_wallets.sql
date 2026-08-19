-- 151: Реестр известных Smart Money кошельков
CREATE TABLE IF NOT EXISTS smart_money_wallets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    address TEXT UNIQUE NOT NULL,
    label TEXT,
    source TEXT DEFAULT 'seed',
    specialization TEXT[] DEFAULT '{}',
    total_pnl NUMERIC DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_smart_money_wallets_address ON smart_money_wallets(address);
CREATE INDEX IF NOT EXISTS idx_smart_money_wallets_active ON smart_money_wallets(is_active) WHERE is_active = TRUE;

-- Seed: известные Smart Money адреса из исследования
INSERT INTO smart_money_wallets (address, label, source, specialization, total_pnl) VALUES
    ('0x17db3fcd93ba12d38382a0cade24b200185c5f6d', 'fengdubiying', 'seed', ARRAY['politics', 'pop_culture'], 2900000),
    ('0xdbade4c82fb72780a0db9a38f821d8671aba9c95', 'SwissMiss', 'seed', ARRAY['sports', 'crypto'], 2800000),
    ('0x9d84ce0306f8551e02efef1680475fc0f1dc1344', 'ImJustKen', 'seed', ARRAY['macro', 'fed'], 2400000),
    ('0xd218e474776403a330142299f7796e8ba32eb5c9', 'Whale Alpha', 'seed', ARRAY['trends'], 900000),
    ('0xee613b3fc183ee44f9da9c05f53e2da107e3debf', 'High Win Rate', 'seed', ARRAY['global_news'], 1300000)
ON CONFLICT (address) DO NOTHING;
