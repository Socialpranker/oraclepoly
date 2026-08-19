-- Objective Data Arbitrage Signals
-- Weather markets phase 1. Category column allows sports/economic later.

CREATE TABLE IF NOT EXISTS objective_signals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id       TEXT NOT NULL,
    question        TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'weather',   -- weather | sports | economic

    -- Распарсенные условия рынка
    city            TEXT,
    airport_station TEXT,                              -- ICAO: KLGA, EGLL, etc.
    temp_threshold  NUMERIC(5, 1),                    -- °F порог из вопроса
    target_date     DATE,

    -- Вероятности
    market_price    NUMERIC(8, 4) NOT NULL,
    meteo_prob      NUMERIC(8, 4) NOT NULL,
    divergence      NUMERIC(8, 4) NOT NULL,            -- meteo_prob - market_price

    -- Snapshot метеоданных
    metar_temp_c    NUMERIC(5, 1),
    forecast_temp_c NUMERIC(5, 1),
    resolution_source TEXT,                            -- NOAA/NWS | WU

    -- Execution
    side            TEXT NOT NULL,                     -- YES | NO
    stake           NUMERIC(8, 2),
    enqueued        BOOLEAN NOT NULL DEFAULT false,

    signal_date     DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (market_id, signal_date)
);

CREATE INDEX IF NOT EXISTS idx_objective_signals_category ON objective_signals (category);
CREATE INDEX IF NOT EXISTS idx_objective_signals_date     ON objective_signals (signal_date);
CREATE INDEX IF NOT EXISTS idx_objective_signals_market   ON objective_signals (market_id);
