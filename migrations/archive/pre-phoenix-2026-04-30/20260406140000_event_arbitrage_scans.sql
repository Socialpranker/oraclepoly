-- Event Arbitrage Scanner: sum-to-1 мониторинг multi-outcome events
CREATE TABLE event_arbitrage_scans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL REFERENCES events(id),
    scanned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    markets_count INT NOT NULL,
    sum_prices NUMERIC(8,6) NOT NULL,
    deviation NUMERIC(8,6) NOT NULL,
    max_spread NUMERIC(8,6),
    outcomes_detail JSONB NOT NULL DEFAULT '[]',
    potential_profit_pct NUMERIC(8,4)
);

CREATE INDEX idx_arb_scans_event ON event_arbitrage_scans(event_id);
CREATE INDEX idx_arb_scans_time ON event_arbitrage_scans(scanned_at);
CREATE INDEX idx_arb_scans_deviation ON event_arbitrage_scans(abs(deviation))
    WHERE abs(deviation) > 0.02;
