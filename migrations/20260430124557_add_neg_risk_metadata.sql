-- 20260430124557_add_neg_risk_metadata.sql
-- Description: Метаданные neg-risk для events — нужны D3 CrossMktArb детектору.
--
-- neg_risk          : true если event помечен Polymarket'ом как neg-risk (mutually exclusive)
-- neg_risk_other    : true если у event есть catch-all market ("Other"/"None of the above") —
--                     в таком случае sum(YES) уже не обязан равняться 1.0, arb-сигнал ложный
-- n_yes_winners     : сколько YES-исходов могут разрешиться победителем одновременно;
--                     для D3 нужны только события с n_yes_winners = 1

ALTER TABLE public.events
    ADD COLUMN IF NOT EXISTS neg_risk boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS neg_risk_other boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS n_yes_winners integer DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_events_neg_risk_eligible
    ON public.events (neg_risk, neg_risk_other, n_yes_winners)
    WHERE neg_risk = true AND neg_risk_other = false AND n_yes_winners = 1;
