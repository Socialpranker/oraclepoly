-- Добавить колонку mode в strategy_positions для разделения dry/live позиций
ALTER TABLE strategy_positions ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'dry';

-- Индекс для фильтрации по mode
CREATE INDEX IF NOT EXISTS idx_strategy_positions_mode ON strategy_positions (strategy_key, market_id, mode, status);
