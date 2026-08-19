-- 20260502141316_register_d7b_detector.sql
-- Register D7b NoFavoriteFade in detector_state for shadow stage tracking.
-- Source: scripts/research/d7b_refined.py canonical backtest 2026-05-02
-- Hypothesis: NO@[0.85,0.95] 4-72h до резолва, exclude Culture, vol≥$5K
-- Expected (canonical, $5K bankroll): daily realistic PnL $82, ROI +68%, WR 15.8%

INSERT INTO public.detector_state (detector, stage) VALUES
    ('D7b_no_favorite_fade', 'shadow')
ON CONFLICT (detector) DO NOTHING;
