-- scoring_configs: versioned Oracle scoring parameters.
-- hash = md5 of deterministic JSON serialization (see app/models/feature_vector.py).

CREATE TABLE IF NOT EXISTS scoring_configs (
    hash TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    weights JSONB NOT NULL,
    formula_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    calibration_source TEXT NOT NULL DEFAULT 'global',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_to_prod_at TIMESTAMPTZ,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_scoring_configs_label ON scoring_configs (label);
CREATE INDEX IF NOT EXISTS idx_scoring_configs_promoted ON scoring_configs (promoted_to_prod_at DESC)
    WHERE promoted_to_prod_at IS NOT NULL;

COMMENT ON TABLE scoring_configs IS
  'Versioned Oracle scoring configs. Phase 1 seeds current prod config as initial entry.';

-- Seed current prod config (values mirror app/core/config.py scoring_w_* at plan write time).
-- Actual values will be re-verified in Phase 2 when scoring is refactored to pure function.
INSERT INTO scoring_configs (hash, label, weights, formula_flags, calibration_source, promoted_to_prod_at, notes)
VALUES (
    'PLACEHOLDER_SEED_HASH',
    'prod_seed_phase1',
    '{"w_whale": 0.8, "w_news": 0.5, "w_sm": 0.6, "w_micro": 0.4, "w_oi": 0.3, "w_conc": 0.2}'::jsonb,
    '{"taker_mode": "contrarian"}'::jsonb,
    'global',
    NOW(),
    'Phase 1 seed. Real config hash computed in Phase 2 after scoring refactor.'
) ON CONFLICT (hash) DO NOTHING;
