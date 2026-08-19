-- 20260322090617_whale_impact_columns.sql
-- Market Impact Score — насколько рынок двигается в сторону ставки кита
-- Три горизонта (1h, 6h, 24h), медиана по сделкам за 30 дней

ALTER TABLE whales ADD COLUMN IF NOT EXISTS market_impact_1h NUMERIC;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS market_impact_6h NUMERIC;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS market_impact_24h NUMERIC;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS impact_sample_size INTEGER;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS impact_updated_at TIMESTAMPTZ;

COMMENT ON COLUMN whales.market_impact_1h IS 'Медиана движения цены через 1ч в сторону ставки кита';
COMMENT ON COLUMN whales.market_impact_6h IS 'Медиана движения цены через 6ч в сторону ставки кита';
COMMENT ON COLUMN whales.market_impact_24h IS 'Медиана движения цены через 24ч в сторону ставки кита';
COMMENT ON COLUMN whales.impact_sample_size IS 'Кол-во сделок в выборке impact';
COMMENT ON COLUMN whales.impact_updated_at IS 'Время последнего расчёта impact';
