-- 20260721120000_create_edge_decay_monitor_config.sql
-- Description: конфиг baseline/recent окон + CUSUM-порогов для edge decay/regime
-- monitor (app/jobs/edge_decay_monitor.py), design doc
-- docs/superpowers/specs/2026-07-21-edge-decay-regime-monitor-design.md.
-- Singleton-таблица (CHECK id=1) — один глобальный набор параметров на все
-- детекторы/методологические классы (не per-entity в этой волне).
--
-- Также расширяет agent_runs.kind CHECK значением 'monitoring' — новая джоба
-- пишет agent_runs(kind='monitoring') на каждую проверенную сущность.

ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_kind_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_kind_check
    CHECK (kind IN ('dq', 'audit', 'experiment', 'proposal', 'monitoring'));

CREATE TABLE IF NOT EXISTS edge_decay_monitor_config (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    baseline_days   INTEGER NOT NULL DEFAULT 90,
    recent_days     INTEGER NOT NULL DEFAULT 14,
    cusum_k         NUMERIC(4,2) NOT NULL DEFAULT 0.5,
    cusum_threshold NUMERIC(4,2) NOT NULL DEFAULT 5.0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO edge_decay_monitor_config (id)
VALUES (1)
ON CONFLICT (id) DO NOTHING;
