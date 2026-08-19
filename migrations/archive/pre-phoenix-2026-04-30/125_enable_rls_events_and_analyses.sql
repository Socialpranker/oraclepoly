-- Enable RLS on events and market_analyses tables
ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_analyses ENABLE ROW LEVEL SECURITY;

-- Events are public read-only (populated by backend service)
CREATE POLICY "events_read_all" ON public.events
    FOR SELECT USING (true);

-- Market analyses are public read-only (populated by backend service)
CREATE POLICY "market_analyses_read_all" ON public.market_analyses
    FOR SELECT USING (true);
