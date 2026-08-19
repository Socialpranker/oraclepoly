-- 145: Score Decomposition — JSONB колонка для прозрачности Oracle Score
-- Хранит разбивку: market_prior, whale_shift, news_shift, swan_impact, top_whales, confidence

ALTER TABLE public.markets
ADD COLUMN IF NOT EXISTS score_decomposition JSONB;
