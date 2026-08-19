-- 20260721144337_create_adverse_selection_monitor_config.sql
-- Description: конфиг окна/порогов для adverse selection intel
-- (app/jobs/adverse_selection_monitor.py), design doc
-- docs/superpowers/specs/2026-07-21-adverse-selection-intel-design.md.
-- Singleton-таблица (CHECK id=1). agent_runs.kind='monitoring' уже в CHECK
-- (миграция 20260721120000) — здесь НЕ расширяем.

CREATE TABLE IF NOT EXISTS adverse_selection_monitor_config (
    id               INTEGER PRIMARY KEY DEFAULT 1,
    lookback_days    INTEGER NOT NULL DEFAULT 60,   -- окно резолвов (как category_performance 60д)
    min_events       INTEGER NOT NULL DEFAULT 20,   -- min N для Wilson-гейта (methods_playbook)
    wilson_threshold NUMERIC(4,2) NOT NULL DEFAULT 0.50,  -- Wilson↓ < этого = проигрыш направления
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO adverse_selection_monitor_config (id)
VALUES (1)
ON CONFLICT (id) DO NOTHING;
