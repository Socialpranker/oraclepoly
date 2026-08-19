-- 20260503200520_register_d14_detector.sql
-- Register D14 CryptoWinnersDistributeNO в detector_state.
-- Source: scripts/research/d14_crypto_winners_4h_canonical.py canonical backtest 2026-05-03
-- Hypothesis: Crypto mid zone [0.50, 0.70) NO-fade на 4h когда winning takers selling
--   - taker_pnl_weighted_usd_24h > 0
--   - taker_roi_weighted_24h > 10%
--   - buy_imbalance_asof < 0.45
-- Canonical numbers:
--   n=2,291, WR 60.2%, net_ROI +13.9%, daily honest $4.84
-- Stage initial = virtual (per user decision 2026-05-03 — все promote in virtual,
-- live только через TG callback + EXECUTION_ENABLED=true).

INSERT INTO public.detector_state (detector, stage) VALUES
    ('D14_crypto_winners_distribute_no', 'virtual')
ON CONFLICT (detector) DO NOTHING;
