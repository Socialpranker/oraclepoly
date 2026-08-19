-- 148: Кастомные алерт-правила пользователей

CREATE TABLE IF NOT EXISTS user_alert_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    market_id UUID REFERENCES markets(id) ON DELETE CASCADE,
    rule_type TEXT NOT NULL,
    direction TEXT,
    threshold_amount NUMERIC,
    threshold_percent NUMERIC,
    channel TEXT NOT NULL DEFAULT 'in_app',
    is_active BOOLEAN NOT NULL DEFAULT true,
    cooldown_minutes INT NOT NULL DEFAULT 30,
    last_triggered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_alert_rules_user ON user_alert_rules(user_id) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_alert_rules_market ON user_alert_rules(market_id) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_alert_rules_type ON user_alert_rules(rule_type) WHERE is_active;

COMMENT ON TABLE user_alert_rules IS 'Пользовательские правила алертов: whale_trade, price_change, oracle_divergence';
