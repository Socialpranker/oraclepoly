-- 168: Crowd Wisdom Index — снимки индекса «мудрости толпы»
-- 4 компонента Шуровьески: diversity, independence, decentralization, aggregation
-- CWI = Π(S_i^w_i) — взвешенное геометрическое среднее ∈ [0, 1]

CREATE TABLE IF NOT EXISTS crowd_wisdom_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id TEXT NOT NULL,

    -- Sub-indices [0.0, 1.0]
    s_diversity NUMERIC(5,4) NOT NULL DEFAULT 0,
    s_independence NUMERIC(5,4) NOT NULL DEFAULT 0,
    s_decentralization NUMERIC(5,4) NOT NULL DEFAULT 0,
    s_aggregation NUMERIC(5,4) NOT NULL DEFAULT 0,

    -- Composite CWI [0.0, 1.0]
    cwi_score NUMERIC(5,4) NOT NULL DEFAULT 0,

    -- Regime: wise | mixed | unstable | mad
    regime TEXT NOT NULL DEFAULT 'unknown',

    -- Raw metrics для backtesting и отладки
    metrics JSONB DEFAULT '{}',

    -- Detected anomalies: [{type, severity, details}]
    anomaly_flags JSONB DEFAULT '[]',

    -- Window stats
    trade_count INTEGER NOT NULL DEFAULT 0,
    unique_traders INTEGER NOT NULL DEFAULT 0,
    window_volume_usd NUMERIC(14,2) NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT chk_cwi_score CHECK (cwi_score >= 0 AND cwi_score <= 1),
    CONSTRAINT chk_s_diversity CHECK (s_diversity >= 0 AND s_diversity <= 1),
    CONSTRAINT chk_s_independence CHECK (s_independence >= 0 AND s_independence <= 1),
    CONSTRAINT chk_s_decentralization CHECK (s_decentralization >= 0 AND s_decentralization <= 1),
    CONSTRAINT chk_s_aggregation CHECK (s_aggregation >= 0 AND s_aggregation <= 1)
);

-- Primary query: history по рынку (Dashboard, backtesting)
CREATE INDEX IF NOT EXISTS idx_cwi_market_time
    ON crowd_wisdom_snapshots(market_id, created_at DESC);

-- Фильтр по режиму: «покажи все MAD-рынки за последний час»
CREATE INDEX IF NOT EXISTS idx_cwi_regime_time
    ON crowd_wisdom_snapshots(regime, created_at DESC);

-- Скоринг: «топ-10 рынков по CWI» для dashboard
CREATE INDEX IF NOT EXISTS idx_cwi_score
    ON crowd_wisdom_snapshots(cwi_score DESC);

-- Quick access columns на markets для API без JOIN
ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS cwi_score NUMERIC(5,4) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS cwi_regime TEXT DEFAULT NULL;

-- Индекс для фильтрации рынков по режиму CWI
CREATE INDEX IF NOT EXISTS idx_markets_cwi_regime
    ON markets(cwi_regime) WHERE cwi_regime IS NOT NULL;

-- ═══════════════════════════════════════════════════════════
-- CWI Paper Portfolio — отдельная модель со своим P&L
-- Отслеживает контр-трендовые позиции при CWI < threshold
-- P&L с учётом: Polymarket fee, gas, slippage
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS cwi_paper_positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id TEXT NOT NULL,

    -- Signal context
    cwi_at_entry NUMERIC(5,4) NOT NULL,
    regime_at_entry TEXT NOT NULL,
    anomaly_type TEXT NOT NULL,             -- HERDING, WHALE_DOMINANCE, LONGSHOT_BIAS, etc.
    signal_details JSONB DEFAULT '{}',      -- полный CWI snapshot при открытии

    -- Position
    side TEXT NOT NULL,                     -- YES или NO
    entry_price NUMERIC(8,6) NOT NULL,     -- цена входа [0, 1]
    stake NUMERIC(12,2) NOT NULL,          -- размер ставки USD
    shares NUMERIC(14,6) NOT NULL,         -- количество акций = stake / entry_price

    -- Fees & Costs (моделируем реальные условия)
    fee_entry NUMERIC(10,4) NOT NULL DEFAULT 0,    -- Polymarket fee при входе
    gas_entry NUMERIC(10,4) NOT NULL DEFAULT 0,     -- gas cost при входе
    slippage_entry NUMERIC(8,6) NOT NULL DEFAULT 0, -- estimated slippage при входе

    -- Exit (заполняется при закрытии)
    exit_price NUMERIC(8,6),
    fee_exit NUMERIC(10,4) DEFAULT 0,
    gas_exit NUMERIC(10,4) DEFAULT 0,

    -- P&L
    gross_pnl NUMERIC(12,4),              -- до вычета комиссий
    net_pnl NUMERIC(12,4),                -- чистый P&L
    total_fees NUMERIC(10,4),             -- суммарные издержки

    -- Status
    status TEXT NOT NULL DEFAULT 'open',   -- open, won, lost, void
    question TEXT,                         -- вопрос рынка для UI

    opened_at TIMESTAMPTZ DEFAULT NOW(),
    closed_at TIMESTAMPTZ,

    CONSTRAINT chk_cwi_paper_side CHECK (side IN ('YES', 'NO')),
    CONSTRAINT chk_cwi_paper_status CHECK (status IN ('open', 'won', 'lost', 'void'))
);

-- Open positions lookup
CREATE INDEX IF NOT EXISTS idx_cwi_paper_market_status
    ON cwi_paper_positions(market_id, status);

-- P&L analytics по времени
CREATE INDEX IF NOT EXISTS idx_cwi_paper_closed_at
    ON cwi_paper_positions(closed_at DESC)
    WHERE status IN ('won', 'lost');

-- Аналитика по типу аномалии (какие сигналы прибыльнее)
CREATE INDEX IF NOT EXISTS idx_cwi_paper_anomaly
    ON cwi_paper_positions(anomaly_type, status);
