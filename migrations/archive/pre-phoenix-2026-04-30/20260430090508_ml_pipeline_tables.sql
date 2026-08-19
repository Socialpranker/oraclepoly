-- Phase 1: ML Pipeline Tables
-- Создаём 3 таблицы для нового ML pipeline:
--   trade_features  — point-in-time фичи для ML
--   ml_predictions  — выходы моделей по версиям
--   ml_models       — регистр обученных моделей
--
-- Не трогаем существующие таблицы. Cutover делается позже.

BEGIN;

-- ============================================================================
-- 1. trade_features — point-in-time фичи для ML обучения и инференса
-- ============================================================================

CREATE TABLE trade_features (
    market_id UUID NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL,

    -- Market features (real-time, всегда есть)
    market_price NUMERIC(8,4) NOT NULL,
    volume_24h NUMERIC(14,2),
    horizon_days NUMERIC(8,2),
    category TEXT,

    -- Microstructure (HF + WebSocket)
    spread_bps NUMERIC(10,2),                  -- bid-ask spread в базисных пунктах
    book_depth_usd NUMERIC(14,2),              -- сумма depth top-5 уровней
    taker_imbalance_30m NUMERIC(8,4),          -- (buy-sell)/total за 30мин
    vpin_60m NUMERIC(8,4),                     -- VPIN toxic flow за 60мин
    liquidity_decay_3h NUMERIC(8,4),           -- скорость испарения liquidity

    -- Whale features (агрегаты)
    whale_buy_volume_24h NUMERIC(14,2),
    whale_sell_volume_24h NUMERIC(14,2),
    whale_unique_count_24h INTEGER,

    -- Smart money features
    sm_cluster_buy_pct NUMERIC(8,4),           -- % из smart money cluster в buy
    sm_pre_resolution_drift NUMERIC(8,4),      -- drift цены за 6h до резолва (rolling)

    -- Cross-market features
    event_market_count INTEGER,
    event_total_volume NUMERIC(14,2),

    -- Outcome (заполняется после resolve)
    outcome SMALLINT,                          -- 0=NO, 1=YES, NULL=pending, -1=void
    resolved_at TIMESTAMPTZ,

    -- Metadata
    feature_version TEXT NOT NULL DEFAULT 'v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (market_id, snapshot_at, feature_version),
    CONSTRAINT trade_features_outcome_chk CHECK (outcome IN (-1, 0, 1) OR outcome IS NULL)
);

-- Hot path: обучение и retrain — берём resolved за период
CREATE INDEX idx_trade_features_resolved_at
    ON trade_features (resolved_at DESC)
    WHERE outcome IS NOT NULL;

-- Hot path: bulk update outcome после резолва
CREATE INDEX idx_trade_features_pending
    ON trade_features (market_id)
    WHERE outcome IS NULL;

-- Категориальный фильтр для per-category models
CREATE INDEX idx_trade_features_category_resolved
    ON trade_features (category, resolved_at DESC)
    WHERE outcome IS NOT NULL;

COMMENT ON TABLE trade_features IS
    'Point-in-time фичи на момент snapshot для ML обучения. Один snapshot = одна строка.';
COMMENT ON COLUMN trade_features.feature_version IS
    'Версия feature engineering. v1 = базовая, v2_with_vpin = с VPIN, etc. PK включает version.';
COMMENT ON COLUMN trade_features.outcome IS
    '0=NO won, 1=YES won, -1=void, NULL=ещё не разрешился.';

-- ============================================================================
-- 2. ml_models — регистр обученных моделей
-- ============================================================================

CREATE TABLE ml_models (
    version TEXT PRIMARY KEY,                  -- 'lgb_v1_2026-04-30_seed42'
    feature_version TEXT NOT NULL,             -- ссылка на trade_features.feature_version

    -- Тренировка
    trained_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    train_period_start TIMESTAMPTZ NOT NULL,
    train_period_end TIMESTAMPTZ NOT NULL,
    train_n INTEGER NOT NULL,                  -- количество строк в train
    test_n INTEGER NOT NULL,

    -- Гиперпараметры (для воспроизводимости)
    model_type TEXT NOT NULL DEFAULT 'lightgbm',
    hyperparams JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Метрики на test split
    test_brier NUMERIC(10,5),
    test_logloss NUMERIC(10,5),
    test_auc NUMERIC(8,4),
    test_ece NUMERIC(10,5),                    -- expected calibration error
    test_no_roi_pct NUMERIC(8,2),              -- ROI на NO-strategy threshold=10%
    test_no_sharpe NUMERIC(8,3),
    metrics_full JSONB,                        -- полная JSON-структура метрик

    -- Артефакт
    artifact_path TEXT,                        -- /root/oracle/ml_artifacts/lgb_v1.pkl
    feature_importance JSONB,

    -- Lifecycle
    is_active BOOLEAN NOT NULL DEFAULT FALSE,  -- единственная активная для inference
    deployed_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    notes TEXT
);

-- Только одна активная модель в каждый момент (per feature_version)
CREATE UNIQUE INDEX idx_ml_models_active_unique
    ON ml_models (feature_version)
    WHERE is_active = TRUE;

CREATE INDEX idx_ml_models_trained_at ON ml_models (trained_at DESC);

COMMENT ON TABLE ml_models IS
    'Registry of trained ML models. Active model per feature_version is loaded by ml_signal_service.';

-- ============================================================================
-- 3. ml_predictions — output моделей, для shadow mode и audit
-- ============================================================================

CREATE TABLE ml_predictions (
    market_id UUID NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL,
    model_version TEXT NOT NULL REFERENCES ml_models(version),

    -- Predictions
    p_yes NUMERIC(8,4) NOT NULL,               -- предсказанная P(YES)
    confidence NUMERIC(8,4),                   -- model confidence (calibrated)

    -- Decomposition для interpretability (top-5 фичей по contribution)
    contributors JSONB,                        -- [{"feature": "market_price", "value": 0.65, "contrib": 0.12}, ...]

    -- Сравнение с реальностью
    market_price_at_snapshot NUMERIC(8,4),     -- денормализуем для удобных query
    divergence NUMERIC(8,4),                   -- p_yes - market_price
    outcome SMALLINT,                          -- из trade_features (NULL пока pending)

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (market_id, snapshot_at, model_version),
    CONSTRAINT ml_predictions_p_yes_range CHECK (p_yes >= 0 AND p_yes <= 1)
);

CREATE INDEX idx_ml_predictions_model_resolved
    ON ml_predictions (model_version, outcome)
    WHERE outcome IS NOT NULL;

CREATE INDEX idx_ml_predictions_market_recent
    ON ml_predictions (market_id, snapshot_at DESC);

COMMENT ON TABLE ml_predictions IS
    'Outputs of ML models. Used by strategies (active model) and shadow audit (all models).';

COMMIT;
