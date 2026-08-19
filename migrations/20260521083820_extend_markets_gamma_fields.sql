-- 20260521083820_extend_markets_gamma_fields.sql
-- Description: Расширить markets table 16 новыми полями из Gamma /markets response.
--
-- Аудит 2026-05-21 (docs/data_audit_2026-05-21.md C.6): из ~90 полей Gamma /markets
-- сейчас persist'им только 16. Top-priority gaps:
--   - UMA dispute state (D43 разморозится) — приоритет 5
--   - Term-structure price changes (D41 free uplift) — приоритет 5
--   - Cohort flags (cyom/restricted/neg_risk_augmented) для filter — приоритет 4
--   - Lifecycle timestamps — диагностика cold-start
--   - raw jsonb — safety-net: всё что не парсим сейчас, сохранится для будущего
--
-- Источник полей: Gamma response sample проверен 2026-05-21 (Trump kiss market).
-- См. https://gamma-api.polymarket.com/markets?conditionId=X — full payload.

-- ─────────────────────────────────────────────────────────────────────
-- UMA Resolution state (D43 ResolutionDispute detector)
-- ─────────────────────────────────────────────────────────────────────
ALTER TABLE public.markets
    ADD COLUMN IF NOT EXISTS uma_resolution_status   text,
    ADD COLUMN IF NOT EXISTS uma_resolution_statuses jsonb,
    ADD COLUMN IF NOT EXISTS uma_bond                numeric(20, 6),
    ADD COLUMN IF NOT EXISTS uma_reward              numeric(20, 6);

COMMENT ON COLUMN public.markets.uma_resolution_status IS
    'UMA текущий статус резолюции: proposed/disputed/resolved/null. Из Gamma .umaResolutionStatus.';
COMMENT ON COLUMN public.markets.uma_resolution_statuses IS
    'История переходов статуса (массив). Из Gamma .umaResolutionStatuses. D43 fade signal при ≥2 dispute.';
COMMENT ON COLUMN public.markets.uma_bond IS
    'UMA bond требуемый для proposed resolution. Высокие bonds = серьёзный спор. Из Gamma .umaBond.';
COMMENT ON COLUMN public.markets.uma_reward IS
    'UMA reward proposer'"'"'у. Из Gamma .umaReward.';

CREATE INDEX IF NOT EXISTS idx_markets_uma_disputed
    ON public.markets (uma_resolution_status)
    WHERE uma_resolution_status IN ('proposed', 'disputed');

-- ─────────────────────────────────────────────────────────────────────
-- Term-structure price changes (D41 Momentum free uplift)
-- ─────────────────────────────────────────────────────────────────────
-- Polymarket уже считает дельты за нас — нам только колонки добавить.
-- Diff per anchor: 1h, 1d, 1w, 1m, 1y. Хранятся как fractional change (e.g. 0.0234 = +2.34%).
ALTER TABLE public.markets
    ADD COLUMN IF NOT EXISTS one_hour_price_change  numeric(8, 6),
    ADD COLUMN IF NOT EXISTS one_day_price_change   numeric(8, 6),
    ADD COLUMN IF NOT EXISTS one_week_price_change  numeric(8, 6),
    ADD COLUMN IF NOT EXISTS one_month_price_change numeric(8, 6),
    ADD COLUMN IF NOT EXISTS one_year_price_change  numeric(8, 6);

COMMENT ON COLUMN public.markets.one_hour_price_change IS
    'Fractional yes_price change за последний час. Из Gamma .oneHourPriceChange.';
COMMENT ON COLUMN public.markets.one_day_price_change IS
    'Fractional yes_price change за 24h. Из Gamma .oneDayPriceChange.';

-- ─────────────────────────────────────────────────────────────────────
-- Cohort flags (filter в всех детекторах)
-- ─────────────────────────────────────────────────────────────────────
ALTER TABLE public.markets
    ADD COLUMN IF NOT EXISTS cyom                  boolean,
    ADD COLUMN IF NOT EXISTS restricted            boolean,
    ADD COLUMN IF NOT EXISTS neg_risk_augmented    boolean,
    ADD COLUMN IF NOT EXISTS automatically_active  boolean;

COMMENT ON COLUMN public.markets.cyom IS
    'Create-your-own-market — user-created market. Дико перекошены, нужен отдельный cohort. Из Gamma .cyom.';
COMMENT ON COLUMN public.markets.restricted IS
    'US-block. Другой liquidity profile. Из Gamma .restricted.';
COMMENT ON COLUMN public.markets.neg_risk_augmented IS
    'NegRisk multi-outcome augmentation flag. Из Gamma .negRiskAugmented.';

CREATE INDEX IF NOT EXISTS idx_markets_cyom
    ON public.markets (cyom)
    WHERE cyom = true AND is_active = true;

-- ─────────────────────────────────────────────────────────────────────
-- Lifecycle timestamps
-- ─────────────────────────────────────────────────────────────────────
ALTER TABLE public.markets
    ADD COLUMN IF NOT EXISTS accepting_orders_timestamp timestamptz,
    ADD COLUMN IF NOT EXISTS deploying_timestamp        timestamptz;

COMMENT ON COLUMN public.markets.accepting_orders_timestamp IS
    'Момент когда CLOB начал accepting orders. Из Gamma .acceptingOrdersTimestamp.';
COMMENT ON COLUMN public.markets.deploying_timestamp IS
    'Момент deploy market на chain. Из Gamma .deployingTimestamp.';

-- ─────────────────────────────────────────────────────────────────────
-- Raw safety-net (всё что не парсим сейчас)
-- ─────────────────────────────────────────────────────────────────────
ALTER TABLE public.markets
    ADD COLUMN IF NOT EXISTS raw jsonb;

COMMENT ON COLUMN public.markets.raw IS
    'Full Gamma /markets response. Safety-net: zero-cost backfill при появлении новых gap'"'"'ов.';

-- Rollback:
-- ALTER TABLE public.markets
--     DROP COLUMN IF EXISTS uma_resolution_status,
--     DROP COLUMN IF EXISTS uma_resolution_statuses,
--     DROP COLUMN IF EXISTS uma_bond,
--     DROP COLUMN IF EXISTS uma_reward,
--     DROP COLUMN IF EXISTS one_hour_price_change,
--     DROP COLUMN IF EXISTS one_day_price_change,
--     DROP COLUMN IF EXISTS one_week_price_change,
--     DROP COLUMN IF EXISTS one_month_price_change,
--     DROP COLUMN IF EXISTS one_year_price_change,
--     DROP COLUMN IF EXISTS cyom,
--     DROP COLUMN IF EXISTS restricted,
--     DROP COLUMN IF EXISTS neg_risk_augmented,
--     DROP COLUMN IF EXISTS automatically_active,
--     DROP COLUMN IF EXISTS accepting_orders_timestamp,
--     DROP COLUMN IF EXISTS deploying_timestamp,
--     DROP COLUMN IF EXISTS raw;
