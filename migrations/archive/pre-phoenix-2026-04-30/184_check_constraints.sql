-- 182: CHECK constraints для enum-like text колонок
--
-- Аудит 2026-03-19: 12 text колонок без CHECK.
-- Добавляем constraints для 9 колонок с чётко определёнными значениями.
-- Пропускаем: whales.trading_pattern, whales.wallet_classification (LLM-generated, набор не фиксирован),
--             notifications.type (расширяемый).

-- transactions.outcome: YES/NO (Polymarket outcome tokens)
ALTER TABLE transactions ADD CONSTRAINT transactions_outcome_check
    CHECK (outcome IS NULL OR outcome IN ('YES', 'NO'));

-- payments.status: жизненный цикл платежа
ALTER TABLE payments ADD CONSTRAINT payments_status_check
    CHECK (status IN ('pending', 'processed', 'failed', 'refunded'));

-- payments.type: тип покупки
ALTER TABLE payments ADD CONSTRAINT payments_type_check
    CHECK (type IN ('subscription', 'credit_pack'));

-- user_subscriptions.status: жизненный цикл подписки
ALTER TABLE user_subscriptions ADD CONSTRAINT user_subscriptions_status_check
    CHECK (status IN ('active', 'cancelled', 'expired', 'past_due'));

-- user_alert_rules.rule_type: типы алертов (из VALID_RULE_TYPES в alerts.py)
ALTER TABLE user_alert_rules ADD CONSTRAINT user_alert_rules_rule_type_check
    CHECK (rule_type IN ('whale_trade', 'price_change', 'oracle_divergence', 'price_move', 'volume_spike'));

-- user_alert_rules.direction: направление сделки
ALTER TABLE user_alert_rules ADD CONSTRAINT user_alert_rules_direction_check
    CHECK (direction IS NULL OR direction IN ('buy', 'sell', 'any'));

-- user_market_unlocks.tab_type: тип разблокированного таба
ALTER TABLE user_market_unlocks ADD CONSTRAINT user_market_unlocks_tab_type_check
    CHECK (tab_type IN ('all', 'inside', 'news', 'tweets', 'whale'));

-- divergence_signal: oracle vs market price signal
ALTER TABLE oracle_snapshots ADD CONSTRAINT oracle_snapshots_divergence_signal_check
    CHECK (divergence_signal IS NULL OR divergence_signal IN ('fair', 'underpriced', 'overpriced'));

ALTER TABLE vote_snapshots ADD CONSTRAINT vote_snapshots_divergence_signal_check
    CHECK (divergence_signal IS NULL OR divergence_signal IN ('fair', 'underpriced', 'overpriced'));
