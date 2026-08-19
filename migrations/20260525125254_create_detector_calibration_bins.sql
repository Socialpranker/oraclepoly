-- 20260525125254_create_detector_calibration_bins.sql
-- Description: Per-(detector × category × price_bin) historical WR lookup для G18 calibration edge.
--
-- G18 (spec: docs/specs/G18_calibration_edge.md) заменяет hardcoded confidence
-- детекторов на empirically observed historical WR в том же price bin.
-- Backtest на 22,565 resolved signals: Δ Brier -0.0355 (17.6% improvement),
-- PASS verdict (docs/backtests/G18_calibration_edge.md).
--
-- Refit weekly: app/jobs/calibration_bins_refit.py пересчитывает таблицу
-- из detector_signals last 90d.
--
-- Используется в app/services/calibration_service.py (feature flag
-- CALIBRATION_BINS_ENABLED). Не подменяет calibration pipeline целиком,
-- а служит дополнительным tier между Redis cache и hardcoded fallback.

CREATE TABLE IF NOT EXISTS public.detector_calibration_bins (
    detector            text          NOT NULL,
    category            text          NOT NULL,  -- 'Politics', 'Sports', 'Other', 'unknown' и т.д.
    price_bin           numeric(3, 1) NOT NULL CHECK (price_bin >= 0 AND price_bin <= 0.9),
    n_observations      integer       NOT NULL CHECK (n_observations >= 0),
    n_wins              integer       NOT NULL CHECK (n_wins >= 0 AND n_wins <= n_observations),
    wr_observed         numeric(5, 4) NOT NULL,
    wilson_lower_95     numeric(5, 4) NOT NULL,
    wilson_upper_95     numeric(5, 4) NOT NULL,
    sample_window_start timestamptz   NOT NULL,
    sample_window_end   timestamptz   NOT NULL,
    refit_at            timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (detector, category, price_bin)
);

-- Lookup pattern: WHERE detector = $1 AND category = $2 AND price_bin = $3
-- PK уже покрывает 100% queries. Один helper index по refit_at — для analytics
-- "когда последний раз обновляли calibration", и cleanup старых строк.
CREATE INDEX IF NOT EXISTS idx_detector_calibration_bins_refit
    ON public.detector_calibration_bins (refit_at DESC);

COMMENT ON TABLE public.detector_calibration_bins IS
    'G18 calibration edge: per-(detector × category × price_bin) historical WR. Refit weekly. See docs/specs/G18_calibration_edge.md';
