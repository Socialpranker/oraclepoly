-- 20260602110000_create_ml_accuracy_snapshots.sql
-- Description: периодические снимки панели «точности» ML (ml_accuracy_report).
--
-- Cron ml-accuracy-snapshot (weekly) считает honest-cohort метрики по 3 слоям
-- (честность вероятности / куда ставить / когда) + деньги per-category и пишет
-- сюда flatten-снимок. Дашборд (Diagnostics → ML Accuracy) читает последний снимок
-- + строит тренд недели-к-неделе.
--
-- Денормализованный «long» формат (одна строка = одна метрика) — гибко под разные
-- слои без ALTER при добавлении метрик. Структурные детали (segments, reasons) — в meta.
--
-- См. scripts/research/ml_accuracy_report.py + app/jobs/ml_accuracy_snapshot.py.

CREATE TABLE IF NOT EXISTS ml_accuracy_snapshots (
    id            BIGSERIAL    PRIMARY KEY,
    snapshot_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    layer         TEXT         NOT NULL,   -- 'honesty' | 'selection' | 'timing' | 'money'
    detector      TEXT         NOT NULL,   -- 'STANDALONE_V2_LGB' | 'D36_obi_pressure' | ...
    scope         TEXT         NOT NULL,   -- anchor ('24h'/'72h') ИЛИ category ('Sports')
    metric_key    TEXT         NOT NULL,   -- 'per_event_brier' | 'avg_roi_net_pct' | ...
    metric_value  DOUBLE PRECISION,        -- NULL допустимо (например AUC при одном классе)
    cohort_n      INTEGER,                 -- размер когорты (rows или signals)
    n_events      INTEGER,                 -- честный N (уникальные события)
    reliability   TEXT,                    -- 'RELIABLE' | 'WEAK' | 'ARTIFACT' (для money)
    meta          JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- segments, artifact_reasons, etc
    CONSTRAINT ml_accuracy_snapshots_layer_check
        CHECK (layer IN ('honesty', 'selection', 'timing', 'money'))
);

-- Дашборд читает последний снимок: WHERE snapshot_at = (max) ORDER BY layer, detector.
CREATE INDEX IF NOT EXISTS idx_ml_accuracy_snapshots_recent
    ON ml_accuracy_snapshots (snapshot_at DESC, layer);

-- Тренд по конкретной метрике: WHERE detector=? AND scope=? AND metric_key=? ORDER BY snapshot_at.
CREATE INDEX IF NOT EXISTS idx_ml_accuracy_snapshots_trend
    ON ml_accuracy_snapshots (detector, scope, metric_key, snapshot_at DESC);
