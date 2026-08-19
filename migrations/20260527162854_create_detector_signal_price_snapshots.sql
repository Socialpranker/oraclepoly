-- 20260527162854_create_detector_signal_price_snapshots.sql
-- Description: Intraday price snapshots для shadow TP/SL simulation.
--
-- Решает H3 gap из research/shadow-gates-methodology-2026-05/2026-05-27_validation.md:
-- без intraday price-history между signal.generated_at и signal.resolved_at
-- мы не можем симулировать TP/SL exit на shadow → промоушн-гейты слепы к
-- реальной экономике exits.
--
-- Дизайн: каждые 5 минут cron writer берёт все unresolved signals,
-- INSERT'ит current price из markets.current_price в snapshot.
-- Foreign key на detector_signals.id (cascading delete если signal удалён).
--
-- Retention: 30 days hot. После — DELETE через cleanup cron (отдельная
-- следующая итерация). Storage estimate: 27 detectors × ~50 signals/d ×
-- 144 snapshots/d × 30d × 60 bytes = ~350 MB. Acceptable.

CREATE TABLE IF NOT EXISTS detector_signal_price_snapshots (
    detector_signal_id  uuid          NOT NULL REFERENCES detector_signals(id) ON DELETE CASCADE,
    captured_at         timestamptz   NOT NULL DEFAULT now(),
    yes_price           numeric(8, 6) NOT NULL,
    PRIMARY KEY (detector_signal_id, captured_at)
);

COMMENT ON TABLE detector_signal_price_snapshots IS
    'Intraday yes_price snapshots between detector_signal.generated_at and resolved_at. '
    'Written by signal-price-snapshot-writer cron every 5 minutes for all unresolved signals. '
    'Used by shadow TP/SL simulation to compute honest hold-period PnL. '
    'Retention: 30 days (cascading delete on detector_signals.id).';

-- Hot-path index: latest snapshot per signal (used by TP/SL sim).
CREATE INDEX IF NOT EXISTS idx_signal_price_snapshots_signal_latest
    ON detector_signal_price_snapshots (detector_signal_id, captured_at DESC);

-- Time-range index: cleanup queries (DELETE WHERE captured_at < now() - interval '30 days').
CREATE INDEX IF NOT EXISTS idx_signal_price_snapshots_captured_at
    ON detector_signal_price_snapshots (captured_at);
