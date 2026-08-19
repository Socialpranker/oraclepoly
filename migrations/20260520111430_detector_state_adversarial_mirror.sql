-- 20260520111430_detector_state_adversarial_mirror.sql
-- Description: Phase 0 Stage 3 — adversarial mirror gate в detector_state.
--
-- Сейчас adversarial_mirror_pass читается из docs/backtests/{detector}.md
-- (нестабильный источник: файл может быть outdated или missing). Перенос в
-- detector_state делает поле управляемым через POST /v2/lifecycle/adversarial_mirror.
--
-- Mandatory invariant Phase 0 Stage 3:
--   adversarial_mirror_pass IS NULL  → MANUAL_OVERRIDE_NEEDED (blocks promote)
--   adversarial_mirror_pass = false → BLOCKED (asymmetry проверена, fail)
--   adversarial_mirror_pass = true  → passes gate
--
-- См. docs/superpowers/plans/2026-05-honest-baseline/03-mandatory-gates.md.

ALTER TABLE detector_state
  ADD COLUMN IF NOT EXISTS adversarial_mirror_pass boolean,
  ADD COLUMN IF NOT EXISTS adversarial_mirror_evaluated_at timestamptz,
  ADD COLUMN IF NOT EXISTS adversarial_mirror_notes text;

COMMENT ON COLUMN detector_state.adversarial_mirror_pass IS
  'Phase 0 Stage 3: true=passed, false=failed, NULL=not_yet_evaluated. '
  'NULL → MANUAL_OVERRIDE_NEEDED (blocks promote). Set via POST /v2/lifecycle/adversarial_mirror.';
