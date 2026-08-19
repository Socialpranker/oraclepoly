-- Dynamic strategies: JSON фильтры из discovery grid search.
-- Позволяет strategy_discovery автоматически регистрировать стратегии.

ALTER TABLE strategy_state ADD COLUMN IF NOT EXISTS filters JSONB;
ALTER TABLE strategy_state ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'code';
-- source: 'code' (app/strategies/*.py), 'discovery' (автоматически из grid search)

COMMENT ON COLUMN strategy_state.filters IS 'JSON фильтры для dynamic strategies (из discovery)';
COMMENT ON COLUMN strategy_state.source IS 'Источник: code | discovery';
