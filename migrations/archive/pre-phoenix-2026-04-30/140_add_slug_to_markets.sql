-- 140: Добавить колонку slug в markets для Apify orderbook cache и deep links
-- oracle_calculator использует markets.slug для microstructure fetch

ALTER TABLE public.markets ADD COLUMN IF NOT EXISTS slug TEXT;

-- Заполнить slug из связанной таблицы events (event.slug → market.slug)
UPDATE public.markets m
SET slug = e.slug
FROM public.events e
WHERE m.event_id = e.id
  AND e.slug IS NOT NULL
  AND m.slug IS NULL;

-- Индекс для быстрого lookup по slug
CREATE INDEX IF NOT EXISTS idx_markets_slug ON public.markets (slug) WHERE slug IS NOT NULL;
