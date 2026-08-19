-- 20260521083821_create_manifold_bets.sql
-- Description: Manifold individual bet history — для D46 Polymarket↔Manifold lead-lag.
--
-- Аудит 2026-05-21 (docs/data_audit_2026-05-21.md C.6 gap #3, приоритет 5):
-- Manifold API возвращает GET /v0/bets?contractId=X с индивидуальными bets
-- (timestamp, amount, prob_before/after, user). Никто публично не скачивает
-- эту матрицу в синхронной форме с Polymarket.
--
-- Strategy: D46 PolymarketManifoldDivergence сейчас работает только на
-- aggregated probability snapshots (manifold_price_snapshots). Знание
-- индивидуальных bets timing/size → lead-lag сигнал 4-12h раньше
-- Polymarket-движения (PDF §11 information edge).

CREATE TABLE IF NOT EXISTS public.manifold_bets (
    bet_id              text PRIMARY KEY,             -- Manifold bet id
    manifold_market_id  text NOT NULL
                            REFERENCES public.manifold_markets(id)
                            ON DELETE CASCADE,
    user_id             text,                         -- Manifold user id
    amount              numeric(20, 6),               -- Mana или USD
    outcome             text,                         -- 'YES' | 'NO'
    prob_before         numeric(5, 4),                -- probability ДО bet
    prob_after          numeric(5, 4),                -- probability ПОСЛЕ bet
    shares              numeric(20, 6),               -- акции куплены
    is_filled           boolean,
    is_redemption       boolean,
    fees                numeric(20, 6),
    created_time        timestamptz NOT NULL,
    raw                 jsonb,                        -- safety-net full response
    captured_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_manifold_bets_market_time
    ON public.manifold_bets (manifold_market_id, created_time DESC);

CREATE INDEX IF NOT EXISTS idx_manifold_bets_user_time
    ON public.manifold_bets (user_id, created_time DESC)
    WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_manifold_bets_created
    ON public.manifold_bets (created_time DESC);

COMMENT ON TABLE public.manifold_bets IS
    'Manifold individual bet history (per market). Pulled incremental via manifold-bets-scrape cron.';

-- Rollback:
-- DROP TABLE IF EXISTS public.manifold_bets;
