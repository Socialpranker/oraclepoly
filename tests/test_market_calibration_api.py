"""Tests for /api/calibration endpoints (unit, no DB)."""
from unittest.mock import AsyncMock, patch

import pytest

from app.api.calibration import calibration_by_category, calibration_curve, calibration_edges


@pytest.mark.asyncio
async def test_calibration_curve_returns_items_and_count():
    fake_rows = [
        {"horizon_bucket": "far", "price_bucket": 10, "category": "Crypto",
         "n": 50, "implied_prob": 0.50, "actual_winrate": 0.54, "calib_gap": 0.04,
         "brier": 0.24, "wilson_lower": 0.40, "wilson_upper": 0.67},
    ]
    with patch("app.api.calibration.fetch", new=AsyncMock(return_value=fake_rows)):
        result = await calibration_curve(horizon="far", _admin={})
    assert result["count"] == 1
    assert result["items"][0]["calib_gap"] == 0.04


@pytest.mark.asyncio
async def test_calibration_edges_filters_significant_only():
    # edges endpoint должен слать SQL c n>=40 и Wilson не пересекает 0
    with patch("app.api.calibration.fetch", new=AsyncMock(return_value=[])) as mock_fetch:
        await calibration_edges(_admin={})
    sql = mock_fetch.await_args.args[0]
    assert "n >= 40" in sql
    # значимость: либо wilson_upper < implied (YES переоценён), либо wilson_lower > implied
    assert "wilson_upper" in sql and "wilson_lower" in sql


@pytest.mark.asyncio
async def test_calibration_by_category_filters_min_sample():
    with patch("app.api.calibration.fetch", new=AsyncMock(return_value=[])) as mock_fetch:
        result = await calibration_by_category(_admin={})
    sql = mock_fetch.await_args.args[0]
    assert "GROUP BY category" in sql
    assert "HAVING SUM(n) >= 40" in sql
    assert result["count"] == 0
