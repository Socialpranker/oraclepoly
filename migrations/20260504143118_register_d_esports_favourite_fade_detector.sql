-- 20260504143118_register_d_esports_favourite_fade_detector.sql
-- Register D_esports_favourite_fade в detector_state.
-- Source: scripts/research/d_esports_favourite_fade_canonical.py canonical backtest 2026-05-04
-- Hypothesis: Other category esports markets (LoL/Dota/CS/Valorant/CoD/HoK), mid zone
--   [0.50, 0.70), 4h anchor, NO side, когда price_min_asof < 0.15 (lifetime touched
--   near-zero — esports intra-game volatility selection).
-- Canonical numbers (821-day backtest):
--   n=504, WR 70.2%, net_ROI +58.7%, mirror_diff +111.4pp, daily honest $4.50 (~$1,643/year)
--   Year split 2025 +58% / 2026 +60% (consistency 0.97)
--   Liquidity-robust (vol≥$100K → +69%), slippage-robust (5pp slip → +48%)
-- Discovered via Auto Edge Scanner top-1 finding (id=528, scan 2026-05-03) → manual
-- review (memory/d_top1_drawdown_review_2026-05-04.md) → drawdown proxy replaced
-- with cleaner price_min_asof feature.
-- Stage initial = shadow (новый detector — observe only first, promote to virtual
-- через TG callback после 1-2 weeks live shadow).

INSERT INTO public.detector_state (detector, stage) VALUES
    ('D_esports_favourite_fade', 'shadow')
ON CONFLICT (detector) DO NOTHING;
