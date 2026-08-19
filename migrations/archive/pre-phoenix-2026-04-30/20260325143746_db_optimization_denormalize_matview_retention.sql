-- DB Optimization: Phase 3 (денормализация) + Phase 4 (matview) + Phase 5 (retention)

-- === Phase 3: Денормализация category + end_date в oracle_snapshots ===
-- Устраняет 12+ JOIN oracle_snapshots↔markets в горячих запросах

ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE oracle_snapshots ADD COLUMN IF NOT EXISTS end_date TIMESTAMPTZ;

-- Backfill из markets (одноразовая операция)
UPDATE oracle_snapshots os
SET category = m.category, end_date = m.end_date
FROM markets m
WHERE m.id = os.market_id
  AND os.category IS NULL;

-- Индекс для запросов по категории
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_oracle_snapshots_category
ON oracle_snapshots (category) WHERE category IS NOT NULL;

-- === Phase 4: Materialized View для resolved outcomes ===
-- Заменяет DISTINCT ON (market_id) по 2.8M строк (7.8 сек → 10 мс)

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_market_resolved_outcome AS
SELECT DISTINCT ON (market_id)
    market_id,
    outcome,
    resolved_at
FROM oracle_snapshots
WHERE outcome != 'pending'
ORDER BY market_id, resolved_at DESC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_market_resolved_outcome_market_id
ON mv_market_resolved_outcome (market_id);

-- === Phase 5: Retention — удаление старых pending snapshots ===
-- pending > 14 дней — рынок давно не resolved, snapshots бесполезны
-- resolved snapshots сохраняем для calibration (90 дней)

DELETE FROM oracle_snapshots
WHERE prediction_correct IS NULL
  AND outcome = 'pending'
  AND created_at < NOW() - INTERVAL '14 days';

-- market_analyses для неактивных рынков
DELETE FROM market_analyses
WHERE market_id IN (
    SELECT id FROM markets WHERE is_active = false
);
