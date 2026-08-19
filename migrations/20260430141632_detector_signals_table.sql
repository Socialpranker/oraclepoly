-- 20260430141632_detector_signals_table.sql
-- Description: Shadow-mode storage для детекторов D1/D3.
--
-- Каждый Signal эмитится детектором → INSERT сюда → раз в час cron resolve
-- посчитает outcome (WIN/LOSS/EXPIRED) + realized_roi. На этих данных потом
-- учим L1 Beta calibrator (Phase E).
--
-- Уникальность: один и тот же tx_hash whale-event'а может прилететь дважды
-- (WS + polling). UNIQUE на (detector, market_id, evidence->>'tx_hash')
-- предотвращает дубли D1. Для D3 tx_hash null — uniqueness через generated_at
-- + market_id (одну минуту на market — один сигнал).

CREATE TABLE IF NOT EXISTS public.detector_signals (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    detector        text        NOT NULL,
    market_id       text        NOT NULL,
    event_id        text,
    side            text        NOT NULL CHECK (side IN ('YES', 'NO')),
    confidence      numeric(5,4) NOT NULL,
    expected_edge_pct numeric(7,2) NOT NULL,
    fair_price      numeric(6,4) NOT NULL,
    current_price   numeric(6,4) NOT NULL,
    horizon_sec     integer     NOT NULL,
    token_id        text,
    evidence        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    generated_at    timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),

    -- Resolved fields (заполняются cron'ом detector-signals-resolve)
    resolved        boolean     NOT NULL DEFAULT false,
    outcome         text        CHECK (outcome IS NULL OR outcome IN ('WIN', 'LOSS', 'EXPIRED')),
    realized_roi    numeric(8,3),
    resolved_at     timestamptz
);

CREATE INDEX IF NOT EXISTS idx_detector_signals_generated_at
    ON public.detector_signals (generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_detector_signals_unresolved
    ON public.detector_signals (resolved, generated_at)
    WHERE resolved = false;

CREATE INDEX IF NOT EXISTS idx_detector_signals_detector_market
    ON public.detector_signals (detector, market_id, generated_at DESC);

-- D1 idempotency: уникальность по tx_hash whale-события
CREATE UNIQUE INDEX IF NOT EXISTS uq_detector_signals_d1_tx
    ON public.detector_signals (detector, (evidence->>'tx_hash'))
    WHERE detector = 'D1_fade_whale' AND evidence->>'tx_hash' IS NOT NULL;

-- D3 idempotency через app-level guard: ON CONFLICT DO NOTHING в DetectorSignalRepository.
-- D3 scan cron работает раз в 10 мин, race window <1s (между fetch active markets и INSERT).
-- DB-level UNIQUE на minute-bucket был removed: timestamptz functions in index expression
-- помечены как STABLE postgres'ом, не IMMUTABLE. Если расширим частоту scan'а до <1min —
-- добавить generated column для bucket bigint.
