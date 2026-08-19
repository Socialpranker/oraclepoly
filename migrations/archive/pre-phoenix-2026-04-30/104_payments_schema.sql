-- 104: Payment system tables for CryptoBot (Crypto Pay) integration
-- Subscription plans, credit packs, user subscriptions, and payment history

-- Subscription plans (configurable pricing)
CREATE TABLE IF NOT EXISTS subscription_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,           -- 'standard', 'pro'
    display_name TEXT NOT NULL,          -- 'Standard', 'Pro'
    price_usd NUMERIC(10,2) NOT NULL,
    credits_per_month INT NOT NULL,
    features JSONB DEFAULT '[]',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- One-time credit packs
CREATE TABLE IF NOT EXISTS credit_packs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    credits INT NOT NULL,
    price_usd NUMERIC(10,2) NOT NULL,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- User subscriptions (active/cancelled/expired)
CREATE TABLE IF NOT EXISTS user_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    plan_id UUID NOT NULL REFERENCES subscription_plans(id),
    status TEXT NOT NULL DEFAULT 'active',  -- active, cancelled, expired, past_due
    reminder_sent_at TIMESTAMPTZ,           -- when renewal reminder was sent
    current_period_start TIMESTAMPTZ NOT NULL DEFAULT now(),
    current_period_end TIMESTAMPTZ NOT NULL,
    cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Payment history (all transactions)
CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    type TEXT NOT NULL,                    -- 'subscription', 'credit_pack'
    amount_usd NUMERIC(10,2) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'pending', -- pending, processed, failed, refunded
    cryptopay_invoice_id BIGINT UNIQUE,    -- CryptoBot invoice ID
    pay_url TEXT,                          -- CryptoBot payment URL for reuse
    merchant_tx_id TEXT NOT NULL UNIQUE,   -- our idempotency key
    metadata JSONB DEFAULT '{}',           -- plan_id/pack_id, telegram_id, etc.
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_merchant_tx ON payments(merchant_tx_id);
CREATE INDEX IF NOT EXISTS idx_payments_cryptopay_invoice ON payments(cryptopay_invoice_id);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_user ON user_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_status ON user_subscriptions(status);

-- Atomic credit granting (counterpart to deduct_credit)
CREATE OR REPLACE FUNCTION grant_credits(p_telegram_id BIGINT, p_amount INT)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_new_credits INT;
BEGIN
    UPDATE users
    SET credits = credits + p_amount
    WHERE telegram_id = p_telegram_id
    RETURNING credits INTO v_new_credits;

    IF NOT FOUND THEN
        RETURN -1;
    END IF;

    RETURN v_new_credits;
END;
$$;

-- Auto-update updated_at on row modification
CREATE OR REPLACE FUNCTION update_payments_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_user_subscriptions_updated_at
    BEFORE UPDATE ON user_subscriptions
    FOR EACH ROW EXECUTE FUNCTION update_payments_updated_at();

CREATE TRIGGER trg_payments_updated_at
    BEFORE UPDATE ON payments
    FOR EACH ROW EXECUTE FUNCTION update_payments_updated_at();

-- Seed default plans ($20/$60 pricing)
INSERT INTO subscription_plans (name, display_name, price_usd, credits_per_month, features) VALUES
('standard', 'Standard', 20.00, 10, '["10 Deep Insight Credits", "Whale Activity Alerts", "Basic Oracle Scores"]'),
('pro', 'Pro', 60.00, 30, '["30 Deep Insight Credits", "Black Swan Risk Analysis", "Whale Confidence Metrics", "Priority News Sync"]')
ON CONFLICT (name) DO NOTHING;

INSERT INTO credit_packs (name, credits, price_usd) VALUES
('starter', 5, 10.00),
('standard', 15, 25.00),
('bulk', 40, 60.00)
ON CONFLICT (name) DO NOTHING;
