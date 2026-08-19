-- 20260702151420_ramp_stake_cap.sql
-- Description: кодификация ramp-лесенки (audit research-gap): live_stake_cap_usd в detector_state.
-- Ступени и числа — docs/superpowers/plans/2026-06-25-d3-live-proxy-ramp.md §3.1:
-- S0 канарейка $5-10, S1 $15-20, S2 $30-40, S3 полный (EV-sizer cap, без фикс. потолка).

ALTER TABLE public.detector_state
    ADD COLUMN IF NOT EXISTS live_stake_cap_usd numeric(10,2);

-- S0 canary верхняя граница ($5-10 по ramp-плану §3.1).
UPDATE public.detector_state SET live_stake_cap_usd = 10.00 WHERE stage = 'live';

