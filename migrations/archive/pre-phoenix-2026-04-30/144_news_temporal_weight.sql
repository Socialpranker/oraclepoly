-- Добавить temporal_weight в news_market_mappings.
-- Default 1.0 = multiplicative identity (старые записи ведут себя как раньше).
-- Значения: 0.5 = echo, 1.0 = normal, 1.5 = high alpha.
ALTER TABLE news_market_mappings
ADD COLUMN IF NOT EXISTS temporal_weight FLOAT DEFAULT 1.0;
