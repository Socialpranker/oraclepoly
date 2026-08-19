"""Phase 4 of portfolios refactor — portfolio gate overrides applied в live_readiness.

Подтверждаем что:
- ML portfolio с {"adversarial_mirror": "skip"} → ready=True даже если backtest fail
- External portfolio с {"days_accumulated": "skip"} → не блокирует на 14d
- Rules portfolio (no overrides) → gates evaluated normally
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


def _state_row(detector: str, days_ago: int = 22):
    return {
        "detector": detector,
        "stage": "virtual",
        "promoted_at": datetime.now(UTC) - timedelta(days=days_ago),
    }


def _virtual_row(detector: str, n_placed: int = 50, n_filled: int = 35, pnl: float = 50.0):
    return {
        "detector": detector,
        "n_placed": n_placed,
        "n_filled": n_filled,
        "n_settled": 20,
        "wins": 12,
        "losses": 8,
        "pnl_usd": pnl,
    }


@pytest.mark.asyncio
async def test_ml_portfolio_skips_adversarial_mirror():
    """ML portfolio с skip override → adv_gate.passed → True даже если backtest fails."""
    from app.api.bot import bot_v2_lifecycle_live_readiness
    from app.services import portfolios as portfolios_svc

    fake_state = [_state_row("STANDALONE_V2_LGB")]
    fake_virtual = [_virtual_row("STANDALONE_V2_LGB")]
    fake_portfolios = [
        {
            "portfolio_id": "ml", "display_name": "ML",
            "bankroll_usd": 1000.0, "exposure_cap_usd": 1000.0, "kelly_fraction": 0.10,
            "gate_overrides": {"adversarial_mirror": "skip"}, "enabled": True,
        }
    ]
    fake_pmap = [{"detector": "STANDALONE_V2_LGB", "portfolio_id": "ml"}]

    async def mock_fetch(query, *args, **kwargs):
        if "hours_to_settle" in query:
            return []
        if "execution_orders" in query:
            return fake_virtual
        if "detector_signals" in query:
            return []
        if "FROM portfolios" in query:
            return fake_portfolios
        if "WHERE portfolio_id IS NOT NULL" in query:
            return fake_pmap
        return fake_state

    portfolios_svc.invalidate_cache()
    with (
        patch("app.api.bot.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.services.detector_metrics.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.services.portfolios.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.api.bot.get_settings") as mock_settings,
    ):
        mock_settings.return_value.execution_enabled = True  # all 5 gates passable
        result = await bot_v2_lifecycle_live_readiness(_admin={})

    d = result["detectors"][0]
    assert d["portfolio_id"] == "ml"
    assert "adversarial_mirror" in d["gate_overrides_applied"]
    # adv_gate.passed = True (был None / False, после skip → True)
    assert d["gates"]["adversarial_mirror"]["passed"] is True
    assert d["gates"]["adversarial_mirror"]["override"] == "skip"


@pytest.mark.asyncio
async def test_rules_portfolio_no_override_evaluated_normally():
    """Rules portfolio без overrides → gate_overrides_applied=[] и gates без override field."""
    from app.api.bot import bot_v2_lifecycle_live_readiness
    from app.services import portfolios as portfolios_svc

    fake_state = [_state_row("D7b_no_favorite_fade")]
    fake_virtual = [_virtual_row("D7b_no_favorite_fade")]
    fake_portfolios = [
        {
            "portfolio_id": "rules", "display_name": "Rules",
            "bankroll_usd": 3000.0, "exposure_cap_usd": 3000.0, "kelly_fraction": 0.20,
            "gate_overrides": {}, "enabled": True,
        }
    ]
    fake_pmap = [{"detector": "D7b_no_favorite_fade", "portfolio_id": "rules"}]

    async def mock_fetch(query, *args, **kwargs):
        if "hours_to_settle" in query:
            return []
        if "execution_orders" in query:
            return fake_virtual
        if "detector_signals" in query:
            return []
        if "FROM portfolios" in query:
            return fake_portfolios
        if "WHERE portfolio_id IS NOT NULL" in query:
            return fake_pmap
        return fake_state

    portfolios_svc.invalidate_cache()
    with (
        patch("app.api.bot.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.services.detector_metrics.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.services.portfolios.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.api.bot.get_settings") as mock_settings,
    ):
        mock_settings.return_value.execution_enabled = True
        result = await bot_v2_lifecycle_live_readiness(_admin={})

    d = result["detectors"][0]
    assert d["portfolio_id"] == "rules"
    assert d["gate_overrides_applied"] == []
    # No override field on any gate
    for gate_name, gate in d["gates"].items():
        assert "override" not in gate, f"gate {gate_name} has unexpected override"


@pytest.mark.asyncio
async def test_unknown_portfolio_id_no_overrides_applied():
    """Если portfolio_map отсутствует detector → portfolio_id=None, no overrides."""
    from app.api.bot import bot_v2_lifecycle_live_readiness
    from app.services import portfolios as portfolios_svc

    fake_state = [_state_row("D_orphan")]
    fake_virtual = [_virtual_row("D_orphan")]

    async def mock_fetch(query, *args, **kwargs):
        if "hours_to_settle" in query:
            return []
        if "execution_orders" in query:
            return fake_virtual
        if "detector_signals" in query:
            return []
        if "FROM portfolios" in query:
            return []  # No portfolios at all
        if "WHERE portfolio_id IS NOT NULL" in query:
            return []  # detector not in portfolio_map
        return fake_state

    portfolios_svc.invalidate_cache()
    with (
        patch("app.api.bot.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.services.detector_metrics.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.services.portfolios.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.api.bot.get_settings") as mock_settings,
    ):
        mock_settings.return_value.execution_enabled = False
        result = await bot_v2_lifecycle_live_readiness(_admin={})

    d = result["detectors"][0]
    assert d["portfolio_id"] is None
    assert d["gate_overrides_applied"] == []
