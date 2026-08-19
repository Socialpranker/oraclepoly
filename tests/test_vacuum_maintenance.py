"""Unit тесты для app.jobs.vacuum_maintenance — gate-логика VACUUM FULL markets."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

MODULE = "app.jobs.vacuum_maintenance"
MB = 1024 * 1024


@pytest.mark.asyncio
async def test_skips_when_below_trigger():
    """Размер ниже VACUUM_TRIGGER_MB → skip, VACUUM FULL не вызывается."""
    from app.jobs.vacuum_maintenance import VACUUM_TRIGGER_MB, run_vacuum_maintenance

    below = int((VACUUM_TRIGGER_MB - 100) * MB)
    with (
        patch(f"{MODULE}.fetchval", new_callable=AsyncMock, return_value=below),
        patch(f"{MODULE}.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_vacuum_maintenance()

    assert result["status"] == "skipped_below_trigger"
    assert result["reclaimed_mb"] == 0.0
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_vacuums_when_above_trigger():
    """Размер выше порога → VACUUM FULL вызывается, reclaimed считается before-after."""
    from app.jobs.vacuum_maintenance import VACUUM_TRIGGER_MB, run_vacuum_maintenance

    before = int((VACUUM_TRIGGER_MB + 100) * MB)  # 1050 MB
    after = int((VACUUM_TRIGGER_MB - 150) * MB)  # 800 MB после пересборки
    with (
        patch(f"{MODULE}.fetchval", new_callable=AsyncMock, side_effect=[before, after]),
        patch(f"{MODULE}.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_vacuum_maintenance()

    assert result["status"] == "vacuumed"
    assert result["before_mb"] == pytest.approx(VACUUM_TRIGGER_MB + 100, abs=0.5)
    assert result["after_mb"] == pytest.approx(VACUUM_TRIGGER_MB - 150, abs=0.5)
    assert result["reclaimed_mb"] == pytest.approx(250, abs=0.5)
    mock_exec.assert_awaited_once()


@pytest.mark.asyncio
async def test_uses_vacuum_full_not_plain_vacuum():
    """Регрессия: запускаем именно VACUUM (FULL, ANALYZE), обычный VACUUM не вернёт ОС место."""
    from app.jobs.vacuum_maintenance import VACUUM_TRIGGER_MB, run_vacuum_maintenance

    above = int((VACUUM_TRIGGER_MB + 50) * MB)
    with (
        patch(f"{MODULE}.fetchval", new_callable=AsyncMock, side_effect=[above, above]),
        patch(f"{MODULE}.execute", new_callable=AsyncMock) as mock_exec,
    ):
        await run_vacuum_maintenance()

    sql = mock_exec.await_args.args[0].upper()
    assert "VACUUM" in sql
    assert "FULL" in sql
    assert "MARKETS" in sql


@pytest.mark.asyncio
async def test_vacuum_overrides_pool_command_timeout():
    """Регрессия 2026-07-05: VACUUM FULL длится >30s (command_timeout пула) —
    первый прогон джобы упал по TimeoutError. Запрос обязан идти со своим таймаутом."""
    from app.jobs.vacuum_maintenance import VACUUM_TRIGGER_MB, run_vacuum_maintenance

    above = int((VACUUM_TRIGGER_MB + 50) * MB)
    with (
        patch(f"{MODULE}.fetchval", new_callable=AsyncMock, side_effect=[above, above]),
        patch(f"{MODULE}.execute", new_callable=AsyncMock) as mock_exec,
    ):
        await run_vacuum_maintenance()

    timeout = mock_exec.await_args.kwargs.get("timeout")
    assert timeout is not None and timeout > 30
