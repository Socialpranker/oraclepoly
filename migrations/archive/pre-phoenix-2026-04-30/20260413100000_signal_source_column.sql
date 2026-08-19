-- signal_source: источник сигнала (oracle, contra_bias, market_baseline, etc.)
-- Позволяет фильтровать P&L по source, discovery per source, аналитику.

-- trade_signals
ALTER TABLE trade_signals ADD COLUMN IF NOT EXISTS signal_source TEXT NOT NULL DEFAULT 'oracle';
CREATE INDEX IF NOT EXISTS idx_trade_signals_source ON trade_signals(signal_source);

-- strategy_positions
ALTER TABLE strategy_positions ADD COLUMN IF NOT EXISTS signal_source TEXT NOT NULL DEFAULT 'oracle';
CREATE INDEX IF NOT EXISTS idx_sp_signal_source ON strategy_positions(signal_source);

-- strategy_state: signal_source для стратегий (какой pipeline генерирует данные)
ALTER TABLE strategy_state ADD COLUMN IF NOT EXISTS signal_source TEXT NOT NULL DEFAULT 'oracle';

-- Backfill: стратегии с известным signal_source
UPDATE strategy_state SET signal_source = 'contra_bias'
WHERE strategy_key IN ('fast_no_bias', 'sports_mid_horizon', 'politics_no_bias', 'crypto_no_bias', 'favorite_no');

UPDATE strategy_state SET signal_source = 'market_baseline'
WHERE strategy_key IN ('sports_no_baseline', 'culture_no_baseline');

-- Backfill positions и signals по strategy_key → signal_source
UPDATE strategy_positions sp SET signal_source = ss.signal_source
FROM strategy_state ss WHERE sp.strategy_key = ss.strategy_key AND ss.signal_source != 'oracle';

UPDATE trade_signals ts SET signal_source = ss.signal_source
FROM strategy_state ss WHERE ts.strategy_key = ss.strategy_key AND ss.signal_source != 'oracle';
