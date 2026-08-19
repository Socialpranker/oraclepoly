"""Unit tests для signal_price_snapshot_writer cron job."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.signal_price_snapshot_writer import (
    _COUNT_ELIGIBLE_SQL,
    _SNAPSHOT_INSERT_SQL,
    run_signal_price_snapshot_writer,
)


@pytest.mark.asyncio
async def test_no_eligible_signals_returns_no_signals():
    """Когда нет unresolved signals — exit early без INSERT."""
    fetchval_mock = AsyncMock(return_value=0)

    with patch("app.jobs.signal_price_snapshot_writer.fetchval", fetchval_mock):
        result = await run_signal_price_snapshot_writer()

    assert result == {"n_eligible": 0, "status": "no_signals"}


@pytest.mark.asyncio
async def test_eligible_signals_inserts():
    """Eligible signals → execute INSERT + return n_eligible + status='ok'."""
    fetchval_mock = AsyncMock(return_value=42)
    execute_mock = AsyncMock(return_value="INSERT 0 42")

    with (
        patch("app.jobs.signal_price_snapshot_writer.fetchval", fetchval_mock),
        patch("app.jobs.signal_price_snapshot_writer.execute", execute_mock),
    ):
        result = await run_signal_price_snapshot_writer()

    assert result == {"n_eligible": 42, "status": "ok"}
    execute_mock.assert_called_once()


def test_eligibility_window_keeps_both_bounds():
    """Окно съёмки = union (A) horizon-cap OR (B) end_date-буфер + abs cap.

    Регрессионный guard: лёгкая правка может откатить union к одному bound'у
    (история 2026-05-29 — чистый end_date-only обрезал coverage 753→168). Оба
    предиката + абсолютный 30-дневный потолок должны присутствовать в обоих SQL.
    """
    for sql in (_SNAPSHOT_INSERT_SQL, _COUNT_ELIGIBLE_SQL):
        assert "ds.resolved = false" in sql
        # (A) horizon-cap bound
        assert "ds.horizon_sec * 2" in sql
        # (B) end_date + буфер bound (COALESCE fallback на generated_at)
        assert "COALESCE(m.end_date, ds.generated_at) + interval '6 hours'" in sql
        # union, не AND — оба bound'а через OR
        assert " OR now() <=" in sql
        # абсолютный потолок против зомби-сигналов
        assert "ds.generated_at > now() - interval '30 days'" in sql


@pytest.mark.asyncio
async def test_insert_error_returns_error_status():
    """Если INSERT raised — возвращаем status='error' но не падаем."""
    fetchval_mock = AsyncMock(return_value=10)
    execute_mock = AsyncMock(side_effect=RuntimeError("db down"))

    with (
        patch("app.jobs.signal_price_snapshot_writer.fetchval", fetchval_mock),
        patch("app.jobs.signal_price_snapshot_writer.execute", execute_mock),
    ):
        result = await run_signal_price_snapshot_writer()

    assert result["status"] == "error"
    assert result["n_eligible"] == 10
    assert "db down" in result["error"]
