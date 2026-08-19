-- migrations/20260328_execution_orders.sql
-- Execution Bot: таблица ордеров для реального исполнения на Polymarket

CREATE TABLE IF NOT EXISTS execution_orders (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_key    TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    event_id        TEXT,
    side            TEXT NOT NULL,
    execution_mode  TEXT NOT NULL,
    -- Signal snapshot
    signal_price    NUMERIC(8,4) NOT NULL,
    oracle_score    NUMERIC(6,4),
    divergence      NUMERIC(6,4),
    -- Order
    order_id        TEXT,
    order_type      TEXT NOT NULL,
    limit_price     NUMERIC(8,4),
    stake           NUMERIC(10,2) NOT NULL,
    -- Fill
    fill_price      NUMERIC(8,4),
    filled_qty      NUMERIC(10,2),
    slippage        NUMERIC(6,4),
    fee_paid        NUMERIC(8,4),
    -- Status: pending -> placed -> filled -> settled
    --         pending -> placed -> cancelled
    --         pending -> rejected
    status          TEXT NOT NULL DEFAULT 'pending',
    -- Settlement
    pnl             NUMERIC(10,2),
    outcome         TEXT,
    -- Timestamps
    created_at      TIMESTAMPTZ DEFAULT now(),
    placed_at       TIMESTAMPTZ,
    filled_at       TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ,
    settled_at      TIMESTAMPTZ,
    cancel_reason   TEXT,
    meta            JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_exec_orders_status ON execution_orders(status);
CREATE INDEX IF NOT EXISTS idx_exec_orders_strategy ON execution_orders(strategy_key);
CREATE INDEX IF NOT EXISTS idx_exec_orders_market ON execution_orders(market_id);
CREATE INDEX IF NOT EXISTS idx_exec_orders_placed_at ON execution_orders(placed_at)
    WHERE status = 'placed';

-- Register migration
-- LEGACY: ручная регистрация миграции с устаревшей схемой (version, name).
-- run_migration.py теперь сам пишет в schema_migrations(filename).
-- INSERT INTO schema_migrations (version, name) VALUES ('20260328', 'execution_orders')
-- ON CONFLICT DO NOTHING;
