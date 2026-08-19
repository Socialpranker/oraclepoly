"""sizing_controller: bankroll cache, kelly fraction damping."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core import sizing_controller as sc


@pytest.mark.asyncio
async def test_get_live_bankroll_caches_result():
    """Первый вызов — fetch_balance, второй — кэш."""
    sc._balance_cache["ts"] = 0.0  # сбросить кэш
    sc._balance_cache["value"] = 0.0

    with patch("app.core.sizing_controller.fetch_balance", new=AsyncMock(return_value=547.20)):
        snap = await sc.get_live_bankroll()
        assert snap.bankroll_usd == 547.20
        assert snap.is_sufficient is True

    with patch("app.core.sizing_controller.fetch_balance", new=AsyncMock(return_value=999.0)) as m:
        # сразу второй вызов — должен попасть в кэш и не вызвать fetch_balance
        snap2 = await sc.get_live_bankroll()
        assert snap2.bankroll_usd == 547.20  # из кэша
        assert m.await_count == 0


@pytest.mark.asyncio
async def test_get_live_bankroll_below_floor():
    sc._balance_cache["ts"] = 0.0
    with patch("app.core.sizing_controller.fetch_balance", new=AsyncMock(return_value=20.0)):
        snap = await sc.get_live_bankroll()
        assert snap.bankroll_usd == 20.0
        assert snap.is_sufficient is False


@pytest.mark.asyncio
async def test_get_live_bankroll_none():
    sc._balance_cache["ts"] = 0.0
    with patch("app.core.sizing_controller.fetch_balance", new=AsyncMock(return_value=None)):
        snap = await sc.get_live_bankroll()
        assert snap.bankroll_usd == 0.0
        assert snap.is_sufficient is False


@pytest.mark.asyncio
async def test_kelly_fraction_cold_start_low_n():
    """n_30d < 5 → cold-start значение 0.10."""
    rows = [{"pnl_7d": 0, "pnl_30d": 0, "n_30d": 2}]
    with patch("app.core.sizing_controller.fetch", new=AsyncMock(return_value=rows)):
        kelly = await sc.compute_kelly_fraction_today("D1_fade_whale")
        assert kelly == sc._KELLY_COLD


@pytest.mark.asyncio
async def test_kelly_fraction_negative_7d_floors():
    """pnl_7d < 0 → floor 0.05."""
    rows = [{"pnl_7d": -10.0, "pnl_30d": -5.0, "n_30d": 20}]
    with patch("app.core.sizing_controller.fetch", new=AsyncMock(return_value=rows)):
        kelly = await sc.compute_kelly_fraction_today("D1_fade_whale")
        assert kelly == sc._KELLY_FLOOR


@pytest.mark.asyncio
async def test_kelly_fraction_positive_30d_full():
    """pnl_30d > 0, pnl_7d >= 0 → full 0.25."""
    rows = [{"pnl_7d": 1.0, "pnl_30d": 50.0, "n_30d": 30}]
    with patch("app.core.sizing_controller.fetch", new=AsyncMock(return_value=rows)):
        kelly = await sc.compute_kelly_fraction_today("D1_fade_whale")
        assert kelly == sc._KELLY_FULL


@pytest.mark.asyncio
async def test_kelly_fraction_neutral_returns_cold():
    """pnl_7d >= 0 но pnl_30d <= 0 → cold."""
    rows = [{"pnl_7d": 0.5, "pnl_30d": -2.0, "n_30d": 15}]
    with patch("app.core.sizing_controller.fetch", new=AsyncMock(return_value=rows)):
        kelly = await sc.compute_kelly_fraction_today("D1_fade_whale")
        assert kelly == sc._KELLY_COLD
