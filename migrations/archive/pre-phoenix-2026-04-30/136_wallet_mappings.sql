-- Маппинг EOA ↔ Gnosis Safe Proxy кошельков для Polymarket
-- Нужен для агрегации банкролла (B_est) в Kelly Inversion (§3.2)

CREATE TABLE IF NOT EXISTS wallet_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    eoa TEXT NOT NULL,
    proxy TEXT NOT NULL UNIQUE,
    discovered_at TIMESTAMPTZ DEFAULT now(),
    source TEXT DEFAULT 'profile_api',

    CONSTRAINT wallet_mappings_eoa_proxy_key UNIQUE (eoa, proxy)
);

CREATE INDEX IF NOT EXISTS idx_wallet_mappings_eoa ON wallet_mappings(eoa);
CREATE INDEX IF NOT EXISTS idx_wallet_mappings_proxy ON wallet_mappings(proxy);
