-- 170: CWI Bankroll Management
-- Персистентный банкролл для paper trading: реинвестирование 10% прибыли, вывод 90%

CREATE TABLE IF NOT EXISTS cwi_bank_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    bankroll NUMERIC(14,2) NOT NULL DEFAULT 4000,
    total_withdrawn NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_reinvested NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_losses NUMERIC(14,2) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- Только одна строка
    CONSTRAINT chk_single_row CHECK (id = 1)
);

-- Инициализация
INSERT INTO cwi_bank_state (id, bankroll, total_withdrawn, total_reinvested, total_losses)
VALUES (1, 4000, 0, 0, 0)
ON CONFLICT (id) DO NOTHING;

-- Ledger: история изменений банкролла (для графика equity)
CREATE TABLE IF NOT EXISTS cwi_bank_ledger (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    position_id UUID REFERENCES cwi_paper_positions(id),
    event_type TEXT NOT NULL,     -- 'win', 'loss', 'void'
    pnl NUMERIC(12,4) NOT NULL DEFAULT 0,
    reinvested NUMERIC(12,4) NOT NULL DEFAULT 0,
    withdrawn NUMERIC(12,4) NOT NULL DEFAULT 0,
    bankroll_after NUMERIC(14,2) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cwi_bank_ledger_date
    ON cwi_bank_ledger(date DESC);
