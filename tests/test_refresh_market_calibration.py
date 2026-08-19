from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.refresh_market_calibration import run_refresh_market_calibration


@pytest.mark.asyncio
async def test_refresh_runs_concurrently_and_reports():
    with patch("app.jobs.refresh_market_calibration.execute", new=AsyncMock()) as mock_exec:
        result = await run_refresh_market_calibration()

    # Должен вызвать REFRESH CONCURRENTLY ровно для нашего MV
    mock_exec.assert_awaited_once()
    sql = mock_exec.await_args.args[0]
    assert "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_market_calibration" in sql
    assert result["refreshed"] == 1
    assert result["failed"] == []


@pytest.mark.asyncio
async def test_refresh_failure_is_reported_not_raised():
    with patch("app.jobs.refresh_market_calibration.execute", new=AsyncMock(side_effect=Exception("boom"))):
        result = await run_refresh_market_calibration()

    # Не падаем, а репортим
    assert result["refreshed"] == 0
    assert result["failed"] == ["mv_market_calibration"]
