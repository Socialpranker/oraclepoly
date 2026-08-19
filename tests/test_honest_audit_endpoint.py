"""Тест эндпоинта /api/bot/v2/honest-audit (forward-валидация per detector×category)."""
from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_honest_audit_returns_rows_per_detector_category():
    """SQL-строки → структурированный ответ с verdict/trend/per_day_1k."""
    from app.api.bot import bot_v2_honest_audit

    d0 = _dt.date(2026, 6, 1)
    d1 = _dt.date(2026, 6, 30)
    # days=30 → half_days=15. _half_per_day_1k = sum_roi_prop * 25 / 15 / 5.
    # first_half = 12.0*25/15/5 = +4.0 @$1K ; second_half = -3.0*25/15/5 = -1.0 @$1K.
    # delta = -1.0 - 4.0 = -5.0 ≤ -1.0 (порог) → 📉 деградация.
    fake_rows = [
        {
            "detector": "D14_crypto_winners",
            "category": "crypto",
            "n_dedup": 200,
            "sum_roi_prop": 9.0,
            "n_first_half": 100,
            "sum_roi_prop_first": 12.0,
            "n_second_half": 100,
            "sum_roi_prop_second": -3.0,
            "first_signal": d0,
            "last_signal": d1,
        }
    ]
    with patch("app.api.bot.fetch", new=AsyncMock(return_value=fake_rows)):
        result = await bot_v2_honest_audit(days=30, _admin={})

    assert result["days"] == 30
    row = result["rows"][0]
    assert row["detector"] == "D14_crypto_winners"
    assert row["category"] == "crypto"
    assert row["n_dedup"] == 200
    # span = (d1-d0).days = 29 ; per_day_1k = (9.0*25/29)/5
    assert abs(row["per_day_1k"] - (9.0 * 25 / 29 / 5)) < 1e-6
    assert "verdict" in row
    assert row["trend"] == "📉"


@pytest.mark.asyncio
async def test_honest_audit_empty():
    """Нет resolved-сигналов → пустой rows, без краша."""
    from app.api.bot import bot_v2_honest_audit

    with patch("app.api.bot.fetch", new=AsyncMock(return_value=[])):
        result = await bot_v2_honest_audit(days=30, _admin={})
    assert result["rows"] == []
    assert result["days"] == 30
