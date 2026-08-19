-- Kill D1_fade_whale, D4_small_market_fade, D6_retail_bullish_overshoot, D6b_long_horizon.
--
-- Все 4 детектора FAIL на canonical RA (821-day HF parquet, 2026-05-03):
--   D1: gross ROI +0.6%, net -5.7% после fees (no edge)
--   D4: gross +12.7%, net -1.3% после fees + 4pp slippage (no edge)
--   D6: implementation broken (HORIZON_SEC=12h vs canonical peak 14d+)
--   D6b: marginal на canonical но code path identical to D6
--
-- См. PR #413 — canonical RA scripts + docs/backtests/{D1,D4,D6,D6b}.md
-- См. PR #XXX — этот kill PR
--
-- detector_signals/execution_orders с этими детекторами OSTAVLYAJUTSJA как
-- historical record (не удалять — нужны для pipeline_v2_shadow_predictor analysis).

DELETE FROM detector_state WHERE detector IN (
    'D1_fade_whale',
    'D4_small_market_fade',
    'D6_retail_bullish_overshoot',
    'D6b_long_horizon'
);
