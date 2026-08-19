-- Replace PLACEHOLDER_SEED_HASH with real hash of current prod config.
-- Real hash computed from scoring_config_loader.load_prod_config() at migration time.

INSERT INTO scoring_configs (hash, label, weights, formula_flags, calibration_source, promoted_to_prod_at, notes)
VALUES (
    '7c2f8a87c91f0786',
    'prod',
    '{"w_cluster": 0.0, "w_concentration": 0.0, "w_fresh": 0.0, "w_maker_direction": 0.5, "w_micro": 0.0, "w_mkt": 1.0, "w_oi": 0.05, "w_registry": 0.3, "w_taker_imbalance": 0.0, "w_whale": 0.0, "w_whale_contra": -0.3}'::jsonb,
    '{"taker_mode": "contrarian"}'::jsonb,
    'global',
    NOW(),
    'Real prod config hash, Phase 2 migration. Supersedes prod_seed_phase1 placeholder.'
) ON CONFLICT (hash) DO UPDATE SET
    promoted_to_prod_at = NOW(),
    notes = EXCLUDED.notes;

-- Keep old seed row for audit trail, but mark it as not prod.
UPDATE scoring_configs SET promoted_to_prod_at = NULL, notes = 'Phase 1 placeholder, superseded by real prod hash'
WHERE hash = 'PLACEHOLDER_SEED_HASH';
