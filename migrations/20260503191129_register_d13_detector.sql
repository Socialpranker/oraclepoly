-- 20260503191129_register_d13_detector.sql
-- Register D13 RetailConfidenceFade in detector_state for shadow stage.
-- Source: scripts/research/d13_retail_confidence_canonical.py (PR #417)
-- Hypothesis: smart_money_taker_share < 5% в extreme zone YES@[0.85, 0.95) →
--   NO fade имеет sustained edge на all anchors:
--     4h: WR 16.3%, net_ROI +9.9% (n=1214)
--     24h: WR 15.8%, net_ROI +7.4% (n=1243)
--     72h: WR 16.4%, net_ROI +12.0% (n=1021)
--   Combined honest $/d @ $5K: $5.09
-- Adversarial mirror PASS на all anchors (YES side -13%).

INSERT INTO public.detector_state (detector, stage) VALUES
    ('D13_retail_confidence_fade', 'shadow')
ON CONFLICT (detector) DO NOTHING;
