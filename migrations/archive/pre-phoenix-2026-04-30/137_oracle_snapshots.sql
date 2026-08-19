-- 137_oracle_snapshots.sql
-- Таблица для хранения снимков OracleScore при каждом пересчёте.
-- Используется для валидации формулы и подготовки ML-датасета.

CREATE TABLE IF NOT EXISTS oracle_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id UUID NOT NULL REFERENCES markets(id) ON DELETE CASCADE,

    -- Feature vector (входные данные формулы)
    oracle_score NUMERIC(5,4) NOT NULL,
    sentiment_score NUMERIC(5,4),
    whale_confidence NUMERIC(5,4),
    risk_score NUMERIC(5,4),
    news_impact_correction NUMERIC(7,4) DEFAULT 0,

    -- Контекст рынка в момент снимка
    market_price NUMERIC(5,4) NOT NULL,
    volume_24h NUMERIC,

    -- Метаданные расчёта
    news_count INT DEFAULT 0,
    whale_tx_count INT DEFAULT 0,
    black_swan_count INT DEFAULT 0,

    -- Сигналы
    divergence NUMERIC(7,4),
    divergence_signal TEXT,

    -- Исход (заполняется при закрытии рынка)
    outcome TEXT DEFAULT 'pending' CHECK (outcome IN ('pending', 'yes', 'no', 'void')),
    final_price NUMERIC(5,4),
    resolved_at TIMESTAMPTZ,

    -- Производные метрики (считаются при resolve)
    prediction_correct BOOLEAN,
    brier_score NUMERIC(7,4),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_oracle_snapshots_market_time ON oracle_snapshots(market_id, created_at);
CREATE INDEX idx_oracle_snapshots_outcome ON oracle_snapshots(outcome) WHERE outcome != 'pending';
CREATE INDEX idx_oracle_snapshots_created ON oracle_snapshots(created_at);

COMMENT ON TABLE oracle_snapshots IS 'Снимки OracleScore для валидации формулы и ML-оптимизации';
