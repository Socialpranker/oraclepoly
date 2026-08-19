-- 20260521125450_add_market_competitive_score.sql
-- Description: Денормализованный competitive score на markets.
--
-- Polymarket Gamma отдаёт `competitive` (0..1, liquidity-quality proxy) на
-- event level. Мы уже храним его в events.competitive (numeric(5,4)).
-- Для фильтрации детекторами (filter market candidates by competitive ≥ 0.7
-- = достаточная ликвидность) нужен per-market доступ без JOIN на events.
--
-- Заполнение: market_sync копирует event.competitive в каждый market этого
-- event'а на upsert. Backfill — отдельным SQL после применения миграции.
--
-- Источник: research/gamma-api-2026-05/unique_fields.md (top-2 по value/effort).

ALTER TABLE markets
    ADD COLUMN IF NOT EXISTS competitive NUMERIC(5,4);

COMMENT ON COLUMN markets.competitive IS
    'Polymarket liquidity-quality score 0..1, denormalized from parent event. Filter detector candidates by competitive >= 0.7.';

-- Partial index для частого фильтра competitive >= 0.7 на активных markets.
-- 0.7 — эмпирический порог из docs, отдельный threshold у нас в коде нет.
CREATE INDEX IF NOT EXISTS idx_markets_competitive_active
    ON markets (competitive DESC)
    WHERE is_active = true AND competitive IS NOT NULL;

-- Rollback:
-- DROP INDEX IF EXISTS idx_markets_competitive_active;
-- ALTER TABLE markets DROP COLUMN IF EXISTS competitive;
