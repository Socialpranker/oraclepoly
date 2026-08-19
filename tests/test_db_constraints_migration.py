"""Contract tests для Stage 6 migration — Phase 1 Stage 6.

Тестируем что migration SQL файл содержит правильные constraint clauses.
DB integration (что constraint реально fires на INSERT) проверяется через
smoke test на проде после apply migration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

MIGRATION_PATH = (
    Path(__file__).parent.parent
    / "migrations"
    / "20260520131136_execution_orders_hard_constraints.sql"
)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    return MIGRATION_PATH.read_text()


# --- Contract: 4 CHECK constraints exist ---------------------------------


def test_migration_file_exists(migration_sql):
    """Sanity: migration файл readable."""
    assert len(migration_sql) > 0


def test_stake_max_constraint_present(migration_sql):
    """CHECK stake <= 500.0 — mirror Stage 3 per_bet_cap."""
    assert "execution_orders_stake_max" in migration_sql
    assert "stake <= 500" in migration_sql


def test_status_whitelist_present(migration_sql):
    """CHECK status IN (...) — должен включать все 13 валидных значений."""
    assert "execution_orders_status_whitelist" in migration_sql
    for status in (
        "intent", "placed", "filled", "settled", "rejected", "cancelled",
        "virtual_placed", "virtual_filled", "virtual_settled",
        "virtual_cancelled", "virtual_failed", "dry_run", "pending",
    ):
        assert f"'{status}'" in migration_sql, f"missing status '{status}'"


def test_side_whitelist_present(migration_sql):
    """CHECK side IN ('YES', 'NO')."""
    assert "execution_orders_side_whitelist" in migration_sql
    assert "side IN ('YES', 'NO')" in migration_sql


def test_price_bounds_constraint_present(migration_sql):
    """fill_price + signal_price в (0, 1) — оба covered."""
    assert "execution_orders_price_bounds" in migration_sql
    assert "fill_price > 0 AND fill_price < 1" in migration_sql
    assert "signal_price > 0 AND signal_price < 1" in migration_sql


# --- Contract: rollback completeness -------------------------------------


def test_rollback_drops_all_four_constraints(migration_sql):
    """Rollback section должен иметь DROP для каждого ADD CONSTRAINT.
    Защита от партиал-rollback после деплоя."""
    for cn in (
        "execution_orders_stake_max",
        "execution_orders_status_whitelist",
        "execution_orders_side_whitelist",
        "execution_orders_price_bounds",
    ):
        # Rollback в комментах ('-- ALTER TABLE ... DROP CONSTRAINT ... cn')
        assert f"DROP CONSTRAINT IF EXISTS {cn}" in migration_sql, (
            f"rollback missing для {cn}"
        )


# --- Contract: comments document semantics -------------------------------


def test_each_constraint_has_comment(migration_sql):
    """COMMENT ON CONSTRAINT для каждого constraint — для DBA / future me."""
    for cn in (
        "execution_orders_stake_max",
        "execution_orders_status_whitelist",
        "execution_orders_side_whitelist",
        "execution_orders_price_bounds",
    ):
        assert f"COMMENT ON CONSTRAINT {cn}" in migration_sql, (
            f"comment missing для {cn}"
        )


# --- Idempotency UNIQUE — намеренно НЕ добавлен ---------------------------


def test_migration_does_not_add_redundant_unique(migration_sql):
    """План §5 предлагал CREATE UNIQUE INDEX ... idempotency. Skipped: уже
    есть uq_execution_orders_signal + uq_execution_orders_open_market.
    Документируем что migration НЕ создаёт duplicate UNIQUE."""
    # Migration не должна содержать CREATE UNIQUE INDEX (вне comments).
    sql_uncommented = "\n".join(
        line for line in migration_sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "CREATE UNIQUE INDEX" not in sql_uncommented


def test_migration_filename_matches_timestamp_pattern():
    """Filename: YYYYMMDDHHMMSS_*.sql. Required для schema_migrations ordering."""
    fname = MIGRATION_PATH.name
    # 14 digits + underscore
    assert fname[:14].isdigit()
    assert fname[14] == "_"
    assert fname.endswith(".sql")
