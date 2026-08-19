-- 20260520112037_create_promote_attempts.sql
-- Description: Phase 0 Stage 4 — audit table для всех попыток promote через TG.
--
-- Раньше TG promote callback не проверял gates вообще — любой admin одним
-- тапом мог shadow→virtual→live. Теперь _handle_promote_callback вызывает
-- evaluate_promote_gates ДО UPDATE detector_state.stage и пишет audit row
-- для каждой попытки (allowed или blocked).
--
-- См. docs/superpowers/plans/2026-05-honest-baseline/04-tg-promote-validation.md.

CREATE TABLE IF NOT EXISTS promote_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    attempted_at timestamptz NOT NULL DEFAULT now(),
    detector text NOT NULL,
    from_stage text NOT NULL,
    to_stage text NOT NULL,
    user_id text NOT NULL,
    gates_decision text NOT NULL,
    gates_snapshot jsonb NOT NULL,
    allowed boolean NOT NULL,
    blocked_reason text
);

CREATE INDEX IF NOT EXISTS idx_promote_attempts_detector
  ON promote_attempts(detector, attempted_at DESC);

CREATE INDEX IF NOT EXISTS idx_promote_attempts_user
  ON promote_attempts(user_id, attempted_at DESC);

COMMENT ON TABLE promote_attempts IS
  'Audit log: каждая попытка TG promote (allowed/blocked) с gates snapshot. '
  'Phase 0 Stage 4 — security trail + diagnostic для decision flow.';

COMMENT ON COLUMN promote_attempts.gates_decision IS
  'READY | INSUFFICIENT_DATA | BLOCKED | MANUAL_OVERRIDE_NEEDED';

COMMENT ON COLUMN promote_attempts.allowed IS
  'true → detector_state.stage был UPDATEd. false → попытка заблокирована.';
