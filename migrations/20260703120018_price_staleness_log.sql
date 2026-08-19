-- 20260703120018_price_staleness_log.sql
-- Shadow-телеметрия расхождения db-цены и WS live-цены в денежных точках.
-- План: docs/superpowers/plans/2026-07-03-event-driven-price-pipeline.md
-- Гейт-анализ 3-7 дней → решение о Плане 2 (tick-router). Retention: 30 дней (cleanup job).
CREATE TABLE IF NOT EXISTS price_staleness_log (
    id              bigserial PRIMARY KEY,
    logged_at       timestamptz  NOT NULL DEFAULT now(),
    decision_point  text         NOT NULL,   -- 'sizing' | 'bucket_gate' | 'mtm'
    market_id       text         NOT NULL,
    yes_token_id    text,
    db_price        numeric(8,6) NOT NULL,   -- цена, по которой РЕАЛЬНО принято решение
    live_price      numeric(8,6),            -- WS-midpoint в тот же момент; NULL = кэш пуст
    is_live         boolean      NOT NULL,
    delta_abs       numeric(8,6)             -- |live - db|; NULL если is_live=false
);

CREATE INDEX IF NOT EXISTS idx_psl_logged_at ON price_staleness_log (logged_at);
CREATE INDEX IF NOT EXISTS idx_psl_point_time ON price_staleness_log (decision_point, logged_at);
