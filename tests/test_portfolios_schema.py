"""Phase 1 of portfolios refactor — schema migration tests.

Проверяют что миграции существуют и содержат правильные DDL/DML фрагменты.
Реальная БД не доступна в CI — валидируется текст миграций.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "migrations"

PORTFOLIOS_TABLE_MIG = MIGRATIONS / "20260505135001_create_portfolios_table.sql"
DETECTOR_FK_MIG = MIGRATIONS / "20260505135002_detector_state_add_portfolio_id.sql"
SEED_MIG = MIGRATIONS / "20260505135003_seed_portfolios_and_assign_detectors.sql"


def test_portfolios_table_migration_present():
    assert PORTFOLIOS_TABLE_MIG.exists(), f"missing: {PORTFOLIOS_TABLE_MIG.name}"


def test_detector_state_fk_migration_present():
    assert DETECTOR_FK_MIG.exists(), f"missing: {DETECTOR_FK_MIG.name}"


def test_seed_migration_present():
    assert SEED_MIG.exists(), f"missing: {SEED_MIG.name}"


def test_portfolios_table_has_required_columns():
    sql = PORTFOLIOS_TABLE_MIG.read_text()
    assert "CREATE TABLE IF NOT EXISTS public.portfolios" in sql
    for col in (
        "portfolio_id",
        "display_name",
        "bankroll_usd",
        "exposure_cap_usd",
        "kelly_fraction",
        "gate_overrides",
        "enabled",
        "created_at",
        "updated_at",
    ):
        assert col in sql, f"column missing in portfolios table: {col}"
    assert "PRIMARY KEY" in sql
    assert "jsonb" in sql.lower()


def test_detector_state_fk_migration_adds_column_and_index():
    sql = DETECTOR_FK_MIG.read_text()
    assert "ALTER TABLE public.detector_state" in sql
    assert "ADD COLUMN IF NOT EXISTS portfolio_id" in sql
    assert "REFERENCES public.portfolios(portfolio_id)" in sql
    assert "ON UPDATE CASCADE" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "idx_detector_state_portfolio" in sql


def test_seed_migration_inserts_5_portfolios():
    sql = SEED_MIG.read_text()
    for pid in ("rules", "ml", "sentiment", "whale_copy", "external"):
        assert f"'{pid}'" in sql, f"portfolio missing in seed: {pid}"


def test_seed_migration_assigns_standalone_v2_to_ml():
    sql = SEED_MIG.read_text()
    assert "'STANDALONE_V2_LGB'" in sql
    assert "portfolio_id = 'ml'" in sql


def test_seed_migration_defaults_remaining_to_rules():
    sql = SEED_MIG.read_text()
    assert "portfolio_id = 'rules'" in sql
    assert "WHERE portfolio_id IS NULL" in sql


def test_seed_migration_enforces_not_null_at_end():
    sql = SEED_MIG.read_text()
    # NOT NULL должен enforced ПОСЛЕ backfill
    not_null_pos = sql.find("ALTER COLUMN portfolio_id SET NOT NULL")
    rules_backfill_pos = sql.find("WHERE portfolio_id IS NULL")
    assert not_null_pos > 0, "NOT NULL constraint missing"
    assert rules_backfill_pos > 0
    assert not_null_pos > rules_backfill_pos, "NOT NULL must come after backfill"


def test_seed_caps_match_spec():
    """Проверяем initial cap'ы соответствуют утверждённой конфигурации."""
    sql = SEED_MIG.read_text()
    # rules: bankroll & cap = 3000, kelly 0.20
    assert "3000.00, 3000.00, 0.20" in sql
    # ml: bankroll & cap = 1000, kelly 0.10
    assert "1000.00, 1000.00, 0.10" in sql
    # sentiment & whale_copy: bankroll & cap = 500
    assert sql.count("500.00,  500.00") == 2
    # external: bankroll & cap = 300, kelly 0.05
    assert "300.00,  300.00, 0.05" in sql


def test_seed_gate_overrides_match_spec():
    sql = SEED_MIG.read_text()
    assert '{"adversarial_mirror":"skip"}' in sql, "ml portfolio override missing"
    assert '{"canonical_verdict":"skip"}' in sql, "whale_copy override missing"
    assert '{"days_accumulated":"skip"}' in sql, "external override missing"
