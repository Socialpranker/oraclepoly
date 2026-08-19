"""Unit tests для live_ml_features_builder.

Проверяем:
  1. INSERT_COLUMNS соответствует FEATURE_COLUMNS (schema validation, при импорте)
  2. _build_upsert_sql возвращает синтаксически валидный SQL для каждого anchor
  3. _live_market_filter — корректная WHERE clause без leakage anchor_ts > now
  4. Empty result graceful handling (run_live_ml_features_builder с пустыми tables)
"""

from __future__ import annotations

import pytest

from app.jobs.live_ml_features_builder import (
    INSERT_COLUMNS,
    _build_upsert_sql,
    _live_market_filter,
    _validate_insert_columns_match_kernel,
)
from app.ml.feature_kernel import CANONICAL_ANCHORS, FEATURE_COLUMNS


def test_insert_columns_includes_all_kernel_features_minus_condition_id() -> None:
    """INSERT_COLUMNS = ['market_id', 'anchor_hours'] + (FEATURE_COLUMNS - condition_id - anchor_hours)."""
    assert INSERT_COLUMNS[0] == "market_id"
    assert INSERT_COLUMNS[1] == "anchor_hours"
    assert "condition_id" not in INSERT_COLUMNS  # condition_id маппится в market_id

    expected_rest = [c for c in FEATURE_COLUMNS if c not in ("condition_id", "anchor_hours")]
    assert INSERT_COLUMNS[2:] == expected_rest


def test_validate_insert_columns_match_kernel_passes() -> None:
    """Sanity check вызывается при import. Должна проходить без exception."""
    _validate_insert_columns_match_kernel()  # raises RuntimeError если drift


def test_live_market_filter_excludes_resolved_markets() -> None:
    """Live filter не должен включать resolved markets."""
    f = _live_market_filter(24)
    assert "is_active = true" in f
    assert "resolved = false" in f
    assert "end_date IS NOT NULL" in f


def test_live_market_filter_includes_anchor_ts_check() -> None:
    """Anchor_ts <= now() — критично для no-leak."""
    f = _live_market_filter(24)
    assert "INTERVAL '24 hours'" in f
    assert "<= now()" in f


def test_live_market_filter_uses_correct_anchor_h() -> None:
    """h передаётся правильно в INTERVAL."""
    f4 = _live_market_filter(4)
    f720 = _live_market_filter(720)
    assert "INTERVAL '4 hours'" in f4
    assert "INTERVAL '720 hours'" in f720


@pytest.mark.parametrize("anchor_h", CANONICAL_ANCHORS)
def test_build_upsert_sql_produces_valid_sql_for_each_anchor(anchor_h: int) -> None:
    """SQL для каждого anchor должен содержать ON CONFLICT + правильный INSERT."""
    sql = _build_upsert_sql(anchor_h)
    # Проверка структуры
    assert "INSERT INTO live_ml_features" in sql
    assert "ON CONFLICT (market_id, anchor_hours)" in sql
    assert "DO UPDATE SET" in sql
    assert "EXCLUDED.computed_at" in sql
    # condition_id должен быть переименован в market_id
    assert "f.condition_id AS market_id" in sql
    # Anchor_h должен попасть в WHERE clause
    assert f"INTERVAL '{anchor_h} hours'" in sql


def test_upsert_sql_includes_all_feature_columns() -> None:
    """Все feature columns из INSERT_COLUMNS должны попасть в INSERT и UPDATE."""
    sql = _build_upsert_sql(24)
    # Каждая колонка кроме PK должна быть в EXCLUDED.* clause
    for c in INSERT_COLUMNS[2:]:  # пропускаем market_id, anchor_hours (они PK)
        assert f"EXCLUDED.{c}" in sql, f"Column {c} missing from UPDATE SET"


@pytest.mark.asyncio
async def test_run_handles_db_error_gracefully(monkeypatch) -> None:
    """Если execute() кидает exception — counts[h] = -1, не падает."""
    from app.jobs import live_ml_features_builder as mod

    async def fake_execute_fail(query, *args, **kwargs):
        raise RuntimeError("simulated DB error")

    async def fake_fetchval(query, *args, **kwargs):
        return 0

    monkeypatch.setattr(mod, "execute", fake_execute_fail)
    monkeypatch.setattr(mod, "fetchval", fake_fetchval)

    result = await mod.run_live_ml_features_builder()
    assert result["total_upserted"] == 0
    # Все 5 anchors попытались, все failed (counts[h] = -1)
    assert all(v == -1 for v in result["per_anchor"].values())
    assert len(result["per_anchor"]) == len(CANONICAL_ANCHORS)
