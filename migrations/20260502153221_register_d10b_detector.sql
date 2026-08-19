-- 20260502153221_register_d10b_detector.sql
-- Register D10b ElectionLadder in detector_state for shadow stage tracking.
-- Source: scripts/research/d10_canonical.py + /tmp/d10b_refined.py canonical backtest 2026-05-02.
-- Hypothesis: non-NegRisk multi-leg events с sum(yes_price)≠1.0 ≥ 3%, multi-anchor [4..48]h.
-- Expected (canonical, $5K bankroll): daily realistic PnL $27, ROI +71% NO side, 4233 events.

INSERT INTO public.detector_state (detector, stage) VALUES
    ('D10b_election_ladder', 'shadow')
ON CONFLICT (detector) DO NOTHING;
