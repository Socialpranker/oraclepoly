-- 20260322085140_whale_style.sql
-- Whale Strategy Style — 3-я ось классификации (КАК кит торгует)
-- tier (размер) + segment (происхождение) + style (стратегия)

ALTER TABLE whales ADD COLUMN IF NOT EXISTS whale_style TEXT;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS style_confidence NUMERIC;
ALTER TABLE whales ADD COLUMN IF NOT EXISTS style_updated_at TIMESTAMPTZ;

COMMENT ON COLUMN whales.whale_style IS '8 стратегических стилей: edge_trader, asymmetric, event_trader, bonder, speed_trader, lp_maker, arbitrageur, specialist';
COMMENT ON COLUMN whales.style_confidence IS 'Уверенность классификации [0.0, 1.0]';
COMMENT ON COLUMN whales.style_updated_at IS 'Время последней классификации стиля';
