-- 149: User Alert Preferences — категории и минимальные суммы для whale alerts
CREATE TABLE IF NOT EXISTS user_alert_preferences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    categories JSONB DEFAULT '[]'::jsonb,
    min_amount NUMERIC(20, 6) DEFAULT 5000,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id)
);

CREATE INDEX IF NOT EXISTS idx_user_alert_preferences_user_id ON user_alert_preferences(user_id);

COMMENT ON TABLE user_alert_preferences IS 'Настройки фильтрации whale alerts по категориям рынков';
COMMENT ON COLUMN user_alert_preferences.categories IS 'JSON array категорий: ["politics", "crypto", "sports"]. Пустой массив = все категории';
COMMENT ON COLUMN user_alert_preferences.min_amount IS 'Минимальная сумма транзакции для алерта (USD)';
