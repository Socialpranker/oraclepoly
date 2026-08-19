-- Migration: Create users table for Telegram Mini App
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    display_name TEXT,
    avatar_url TEXT,
    level INTEGER DEFAULT 1,
    xp INTEGER DEFAULT 0,
    credits INTEGER DEFAULT 10,
    is_admin BOOLEAN DEFAULT FALSE,
    last_login_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for quick lookups by telegram_id
CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);

-- Enable RLS
ALTER TABLE users ENABLE ROW LEVEL SECURITY;

-- Policies
CREATE POLICY "Users can read own data" ON users FOR SELECT USING (auth.uid()::text = telegram_id::text OR auth.role() = 'service_role');
CREATE POLICY "Service role full access" ON users FOR ALL USING (auth.role() = 'service_role');

-- Add user_id to transactions table for later attribution
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);
